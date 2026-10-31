"""Real PostgreSQL checks for M12 batch recovery and actual production worker startup."""

import argparse
import copy
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path.cwd().resolve()
sys.path.insert(0, str(ROOT))

from m12_bundle.analysis import write
from m12_bundle.environment import isolated, controlled_http, seed, service_for, ControlledAdapter, vector_for


def execute(output):
    from fastapi.testclient import TestClient
    from app.data_audit import audit
    from app.schemas.governance import RevisionRequest
    from app.task_embedding import plan
    from app.task_pipeline import Pipeline, TaskContext
    from app.task_policy import TaskPolicy, TaskFailure, LostExecution
    from app.upload_models import Review
    from m12_bundle.experiments import application, collect
    checks, env, failure = [], None, None
    result = {'passed': False, 'checks': checks, 'external_model_calls': 0}
    def record(name, passed, **detail):
        checks.append({'name': name, 'passed': bool(passed), **detail})
        write(output / 'checks.json', checks)
        if not passed:
            raise AssertionError(name)
    try:
        with controlled_http(delay=0) as (endpoint, provider), isolated(ROOT, output, endpoint=endpoint) as (connect, env):
            policy = TaskPolicy(lease_seconds=3, heartbeat_seconds=.5, min_call_interval=0)
            service = service_for(connect, policy)
            repo, reports = service.repository, service.reports
            adapter = ControlledAdapter()
            def raw(task_id):
                with repo.connection() as conn:
                    return conn.execute('SELECT * FROM processing_tasks WHERE id=%s', (task_id,)).fetchone()
            def execute_next(chosen=None, chosen_adapter=None):
                task = chosen or repo.claim()
                if task is None: raise AssertionError('No queued synthetic task')
                Pipeline(repo, reports, chosen_adapter or adapter).execute(task)
                return raw(task['id'])
            def submit(number, key):
                rid, body = seed(service, number)
                with patch.dict(os.environ, {'M12_EMBEDDING_BATCH_SIZE': '4'}):
                    job = service.confirm(rid, 'a', Review.model_validate(body), key)
                return job, rid, body
            job, rid, body = submit(1, 'normal')
            record('batch_size_saved_in_task', raw(job['id'])['payload'].get('embedding_batch_size') == 4)
            completed = execute_next()
            unique = len(plan(body['raw_text'])[1]); expected_calls = math.ceil(unique / 4)
            record('batch_writes_one_ready_version', completed['status'] == 'succeeded' and completed['calls_reserved'] == expected_calls,
                   unique_texts=unique, call_reservations=completed['calls_reserved'])
            with repo.connection() as conn:
                chunks = conn.execute('SELECT content,embedding::text AS vector FROM report_chunks WHERE report_id=%s', (rid,)).fetchall()
            import json
            matches = all(all(abs(a - b) < 1e-6 for a, b in zip(json.loads(c['vector']), vector_for(c['content'], int(os.getenv('EMBEDDING_DIMENSIONS', '1024'))))) for c in chunks)
            record('vectors_match_original_chunk_contents', bool(chunks) and matches)
            with TestClient(application(service)) as client:
                replay = client.post(f'/api/tasks/reports/{rid}/confirm?member_id=a', json=body, headers={'Idempotency-Key': 'normal'})
                alias = client.post(f'/api/tasks/reports/{rid}/confirm?member_id=a', json=body, headers={'Idempotency-Key': 'other-key'})
                denied = client.get(f'/api/tasks/{job["id"]}?member_id=b')
                illegal = client.post(f'/api/tasks/reports/{rid}/confirm?member_id=a', json={**body, 'reviewed': False}, headers={'Idempotency-Key': 'invalid'})
            record('batch_idempotent_replay', replay.status_code == alias.status_code == 202 and replay.json()['id'] == alias.json()['id'] == job['id'])
            record('member_scope_not_relaxed', denied.status_code == 404)
            record('invalid_input_counted_as_rejected', illegal.status_code == 422)
            metrics, calls, _ = collect(service, [job['id']], 'probe', 'controlled')
            record('timing_and_call_accounting_complete', metrics[0]['timing_complete'] and len(calls) == expected_calls)

            def crash_and_resume(number, point):
                item, report_id, review = submit(number, point)
                claimed = repo.claim()
                process = subprocess.Popen([sys.executable, str(ROOT / 'm12_bundle/tests/crash_child.py'), '--task-id', item['id'],
                                            '--token', claimed['execution_token'], '--point', point], cwd=ROOT,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                end = time.monotonic() + 30
                while process.poll() is None:
                    repo.heartbeat(item['id'], claimed['execution_token'])
                    if time.monotonic() > end:
                        process.kill(); process.communicate(); raise TimeoutError('M12 crash child timed out')
                    time.sleep(.2)
                text = process.communicate()[0]
                (output / f'crash_{point}.txt').write_text(text, encoding='utf-8')
                if process.returncode != 77: raise AssertionError('Crash hook was not reached: ' + text)
                end = time.monotonic() + 10
                while time.monotonic() < end:
                    restored = repo.claim()
                    if restored:
                        break
                    time.sleep(.2)
                else: raise TimeoutError('M12 lease recovery timed out')
                final = execute_next(restored)
                with repo.connection() as conn:
                    states = conn.execute('SELECT status,count(*) AS n FROM processing_calls WHERE task_id=%s GROUP BY status', (item['id'],)).fetchall()
                states = {r['status']: r['n'] for r in states}
                required = math.ceil(len(plan(review['raw_text'])[1]) / 4)
                return item, final, states, required
            saved, final, states, required = crash_and_resume(2, 'stage_saved')
            record('persisted_batch_reused_after_process_crash', final['status'] == 'succeeded' and final['attempts'] == 2 and final['calls_reserved'] == required,
                   states=states, reservations=final['calls_reserved'])
            measurements, _, _ = collect(service, [saved['id']], 'probe', 'controlled')
            record('killed_attempt_timing_remains_missing', measurements[0]['timed_attempts'] == 1 and not measurements[0]['timing_complete'])
            returned, final, states, required = crash_and_resume(3, 'external_returned')
            record('unsaved_batch_may_resend_and_is_unknown', final['status'] == 'succeeded' and final['calls_reserved'] == required + 1 and states.get('unknown') == 1,
                   states=states, reservations=final['calls_reserved'])

            invalid, invalid_rid, _ = submit(4, 'bad-response')
            class Invalid(ControlledAdapter):
                def embeddings(self, contents, timeout):
                    raise TaskFailure('invalid_embedding', usage={'total_tokens': 13})
            failed = execute_next(chosen_adapter=Invalid())
            with repo.connection() as conn:
                count = conn.execute('SELECT count(*) AS n FROM reports WHERE id=%s', (invalid_rid,)).fetchone()['n']
                call = conn.execute('SELECT status,usage FROM processing_calls WHERE task_id=%s', (invalid['id'],)).fetchone()
            record('invalid_batch_no_visible_half_write', failed['status'] == 'failed' and count == 0)
            record('invalid_batch_usage_and_single_attempt_retained', call['status'] == 'error' and call['usage']['total_tokens'] == 13 and failed['attempts'] == 1)

            revised_body = copy.deepcopy(body)
            revised_body['raw_text'] = revised_body['raw_text'].replace('5.7', '5.8')
            revised_body['metrics'][0]['value'] = '5.8'
            revised_body['metrics'][0]['source_text'] = revised_body['metrics'][0]['source_text'].replace('5.7', '5.8')
            request = RevisionRequest.model_validate({'expected_version': 1, 'reason': 'M12 synthetic revision', 'review': revised_body})
            with patch.dict(os.environ, {'M12_EMBEDDING_BATCH_SIZE': '4'}):
                revision = service.revise(rid, 'a', request, 'revision')
            old = raw(revision['id'])
            claimed = repo.claim()
            revised = execute_next(claimed)
            with repo.connection() as conn:
                current = repo.governance.current(conn, rid, 'a', required=True)
            record('batch_revision_keeps_version_relationship', revised['status'] == 'succeeded' and current['data_version'] == current['index_version'] == 2)
            blocked = False
            try:
                with repo.execution(claimed, claimed['execution_token']): pass
            except LostExecution:
                blocked = True
            record('finished_batch_token_cannot_commit_again', blocked)

            legacy, legacy_rid, legacy_body = submit(5, 'legacy-m11')
            with repo.connection() as conn:
                conn.execute("UPDATE processing_tasks SET payload=payload-'embedding_batch_size' WHERE id=%s", (legacy['id'],))
            legacy_done = execute_next()
            expected = len(plan(legacy_body['raw_text'])[1])
            record('outstanding_m11_task_keeps_single_item_stages', legacy_done['status'] == 'succeeded' and legacy_done['calls_reserved'] == expected)

            proc_job, _, _ = submit(6, 'real-worker-process')
            run = subprocess.run([sys.executable, '-m', 'app.task_worker', '--once'], cwd=ROOT, capture_output=True,
                                 encoding='utf-8', errors='replace', timeout=90)
            (output / 'worker_process.txt').write_text(run.stdout + run.stderr, encoding='utf-8')
            proc_result = raw(proc_job['id'])
            record('production_supervisor_child_process_with_batch4', run.returncode == 0 and proc_result['status'] == 'succeeded')
            check = audit(connect, reports.drafts)
            write(output / 'quality.json', check)
            record('final_quality_rules_pass', check['passed'])
    except Exception as error:
        failure = {'error_type': type(error).__name__, 'message': str(error)[:800]}
    result.update(temporary_schema_cleaned=bool(env and env.get('temporary_schema_cleaned')), failure=failure)
    result['passed'] = failure is None and result['temporary_schema_cleaned'] and bool(checks) and all(c['passed'] for c in checks)
    write(output / 'summary.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = execute(args.output.resolve())
    print({'passed': result['passed'], 'scenarios': len(result['checks']), 'external_model_calls': 0})
    raise SystemExit(0 if result['passed'] else 1)
