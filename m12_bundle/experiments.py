"""Controlled HTTP/DB load and small real embedding task measurements."""

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import patch

from m12_bundle.analysis import write, union_ms, summarize_groups, comparison, chart, csv_file, usage_summary
from m12_bundle.environment import controlled_http, isolated, seed, service_for, Worker, fixture, shared_signature


def application(service):
    from fastapi import FastAPI
    from app.api_errors import install_api_contract
    from app.dependencies import get_task_service
    from app.task_routes import router
    app = FastAPI()
    app.include_router(router)
    install_api_contract(app, configure=False)
    app.dependency_overrides[get_task_service] = lambda: service
    return app


def collect(service, ids, group, region):
    repo = service.repository
    with repo.connection() as conn:
        tasks = conn.execute('SELECT * FROM processing_tasks WHERE id=ANY(%s) ORDER BY created_at', (ids,)).fetchall()
        events = conn.execute('SELECT * FROM processing_events WHERE task_id=ANY(%s) ORDER BY id', (ids,)).fetchall()
        calls = conn.execute('SELECT * FROM processing_calls WHERE task_id=ANY(%s) ORDER BY created_at', (ids,)).fetchall()
    output, config = [], {t['id']: t['payload'].get('config', {}) for t in tasks}
    for task in tasks:
        timeline = [e for e in events if e['task_id'] == task['id']]
        claimed = [e for e in timeline if e['event'] == 'claimed']
        measured = [e for e in timeline if e['event'] == 'attempt_timing']
        categories = {}
        for category in ['database', 'model', 'processing', 'file_io', 'rate_wait']:
            categories[category] = sum(union_ms(e['detail'].get('spans', []), category) for e in measured) if measured else None
        output.append({**{k: task[k] for k in ['id', 'report_id', 'member_id', 'kind', 'generation', 'status', 'attempts',
                        'calls_reserved', 'error_code', 'created_at', 'finished_at']}, 'group': group,
                       'batch_size': task['payload'].get('embedding_batch_size', 1),
                       'end_to_end_ms': (task['finished_at'] - task['created_at']).total_seconds() * 1000 if task['finished_at'] else None,
                       'initial_queue_ms': (claimed[0]['created_at'] - task['created_at']).total_seconds() * 1000 if claimed else None,
                       'timed_attempts': len(measured), 'category_ms': categories,
                       'timing_complete': len(measured) == task['attempts'] and all(not e['detail'].get('dropped_spans') for e in measured)})
    call_rows = [{**{k: c[k] for k in ['id', 'task_id', 'attempt', 'stage', 'prefix', 'status', 'usage', 'error_code', 'created_at', 'finished_at']},
                  'group': group, 'model': config[c['task_id']].get(c['prefix'] + '_MODEL'), 'region': region,
                  'reservation_wall_ms': (c['finished_at'] - c['created_at']).total_seconds() * 1000 if c['finished_at'] else None}
                 for c in calls]
    return output, call_rows, events


def run_group(root, folder, *, n, concurrency, size, workload, endpoint=None, adapter=None, real=False):
    from fastapi.testclient import TestClient
    from app.data_audit import audit
    from app.task_policy import TaskPolicy
    group = f'{workload}_c{concurrency}_b{size}'
    requests, tasks, calls, events = [], [], [], []
    result = {'group': group, 'batch_size': size, 'concurrency': concurrency, 'n': n, 'workload': workload,
              'wall_seconds': 0,
              'passed': False, 'external_model_calls': None if real else 0,
              'measurement_scope': 'TestClient HTTP dispatch + real PostgreSQL + single in-process worker + SDK HTTP; production supervisor process startup excluded'}
    failure, env = None, None
    try:
        with isolated(root, folder, endpoint=endpoint) as (connect, env):
            service = service_for(connect)
            fixtures = [seed(service, i + 1) for i in range(n if workload == 'independent' else 1)]
            manifest = [{'case': i + 1, 'text_sha256': hashlib.sha256(fixture(i + 1)['raw_text'].encode()).hexdigest(),
                         'review': fixture(i + 1)} for i in range(n if workload == 'independent' else 1)]
            write(folder / 'fixtures.json', manifest)
            result['fixture_sha256'] = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            result['task_policy'] = vars(service.repository.policy)
            ids = []
            ids_lock = threading.Lock()
            with patch.dict(os.environ, {'M12_EMBEDDING_BATCH_SIZE': str(size)}):
                with TestClient(application(service), raise_server_exceptions=False) as client, Worker(service, adapter) as worker:
                    begin = time.perf_counter()
                    def submit(i):
                        rid, body = fixtures[i if workload == 'independent' else 0]
                        started = time.perf_counter()
                        response = client.post(f'/api/tasks/reports/{rid}/confirm?member_id=a', json=body,
                                               headers={'Idempotency-Key': f'm12-{workload}-{i}'})
                        accepted = time.perf_counter()
                        record = {'group': group, 'case': i + 1, 'http': response.status_code,
                                  'request_id': response.headers.get('x-request-id'),
                                  'accept_ms': (accepted - started) * 1000, 'client_completion_ms': None}
                        if response.status_code != 202:
                            record['error_code'] = response.json().get('code')
                            return record
                        task = response.json()
                        record['task_id'] = task['id']
                        with ids_lock:
                            ids.append(task['id'])
                        # Client waits for the business terminal state, not merely HTTP 202.
                        deadline = time.monotonic() + service.repository.policy.timeout_seconds + 10
                        while task['status'] not in {'succeeded', 'failed', 'superseded'}:
                            if time.monotonic() >= deadline or worker.failure:
                                break
                            time.sleep(.05)
                            polled = client.get(f'/api/tasks/{task["id"]}?member_id=a')
                            if polled.status_code != 200:
                                record['poll_http'] = polled.status_code
                                break
                            task = polled.json()
                        record['status'] = task['status']
                        if task['status'] in {'succeeded', 'failed', 'superseded'}:
                            record['client_completion_ms'] = (time.perf_counter() - started) * 1000
                        return record
                    with ThreadPoolExecutor(max_workers=concurrency) as pool:
                        futures = [pool.submit(submit, i) for i in range(n)]
                        for future in as_completed(futures):
                            requests.append(future.result())
                            write(folder / 'requests.json', sorted(requests, key=lambda r: r['case']))
                    result['started_monotonic_s'] = begin
                    result['ended_monotonic_s'] = time.perf_counter()
                    result['wall_seconds'] = result['ended_monotonic_s'] - begin
                # Worker exit waits for metric flush; collect after that point.
            tasks, calls, events = collect(service, sorted(set(ids)), group, 'cn-beijing' if real else 'controlled')
            check = audit(connect, service.reports.drafts)
            write(folder / 'quality.json', check)
            with service.repository.connection() as conn:
                counts = [conn.execute('SELECT count(*) AS n FROM ' + name).fetchone()['n']
                          for name in ['reports', 'metrics', 'report_revisions']]
                invalid_versions = conn.execute("SELECT count(*) AS n FROM reports WHERE data_version<>1 OR index_version<>data_version OR index_state<>'ready'").fetchone()['n']
            expected = n if workload == 'independent' else 1
            result.update(business_counts=counts, duplicate_business_records=max(0, counts[0] - expected),
                          version_errors=invalid_versions, quality_passed=check['passed'],
                          expected_business_reports=expected, unique_task_count=len(tasks))
            result['passed'] = (len(requests) == n and all(r.get('http') == 202 and r.get('status') == 'succeeded' for r in requests)
                                and len(tasks) == expected and counts == [expected, expected, expected]
                                and invalid_versions == 0 and check['passed'] and all(t['timing_complete'] for t in tasks))
    except Exception as error:
        failure = {'error_type': type(error).__name__, 'message': str(error)[:500]}
    result['temporary_schema_cleaned'] = bool(env and env.get('temporary_schema_cleaned'))
    result['passed'] = result['passed'] and result['temporary_schema_cleaned'] and failure is None
    result['failure'] = failure
    write(folder / 'tasks.json', tasks)
    write(folder / 'calls.json', calls)
    write(folder / 'events.json', events)
    write(folder / 'summary.json', result)
    return result, requests, tasks, calls


def load(root, output, n):
    before = shared_signature()
    groups, requests, tasks, calls = [], [], [], []
    with controlled_http() as (endpoint, provider):
        # AB/BA order varies by concurrency; fixtures, budget and worker concurrency stay fixed.
        for concurrency in [1, 3, 5]:
            for size in ([1, 4] if concurrency != 3 else [4, 1]):
                print(f'M12 controlled: independent c={concurrency} batch={size} n={n}', flush=True)
                values = run_group(root, output / f'independent_c{concurrency}_b{size}', n=n, concurrency=concurrency,
                                   size=size, workload='independent', endpoint=endpoint)
                group, rq, ts, cs = values
                groups.append(group); requests.extend(rq); tasks.extend(ts); calls.extend(cs)
                if not group['passed']:
                    break
            if not groups[-1]['passed']:
                break
        if len(groups) == 6 and all(g['passed'] for g in groups):
            print('M12 controlled: duplicate submissions, counted separately', flush=True)
            group, rq, ts, cs = run_group(root, output / 'duplicate_c5_b4', n=n, concurrency=5, size=4,
                                         workload='duplicate', endpoint=endpoint)
            groups.append(group); requests.extend(rq); tasks.extend(ts); calls.extend(cs)
        write(output / 'controlled_provider.json', provider)
    after = shared_signature()
    stats = summarize_groups(requests, tasks, calls, groups)
    paired = comparison(stats)
    result = {'passed': len(groups) == 7 and all(g['passed'] for g in groups) and before == after
              and len(paired) == 3 and all(r['comparable_correctness'] for r in paired),
              'full_size_complete': n >= 100, 'independent_requests': sum(g['n'] for g in groups if g['workload'] == 'independent'),
              'duplicate_requests': sum(g['n'] for g in groups if g['workload'] == 'duplicate'),
              'groups': stats, 'comparison': paired, 'shared_rows_unchanged': before == after,
              'shared_before': before, 'shared_after': after, 'external_model_calls': 0,
              'model_usage_source': 'controlled_test_double_not_real_tokens',
              'cold_start_rule': 'No warmup calls; first observed task is included in each independent group.',
              'scope': 'Index confirmation tasks with pre-staged synthetic drafts; no PDF extraction, no production capacity claim; concurrency is bounded outstanding logical tasks, worker concurrency=1.'}
    write(output / 'requests.json', requests); write(output / 'tasks.json', tasks); write(output / 'calls.json', calls)
    write(output / 'summary.json', result)
    csv_file(output / 'comparison.csv', paired, ['concurrency', 'baseline_n', 'candidate_n', 'single_median_ms',
             'batch4_median_ms', 'median_change_percent', 'single_calls', 'batch4_calls', 'single_throughput', 'batch4_throughput', 'comparable_correctness'])
    csv_file(output / 'tasks.csv', tasks, ['group', 'id', 'report_id', 'kind', 'batch_size', 'status', 'attempts', 'calls_reserved',
             'end_to_end_ms', 'initial_queue_ms', 'timed_attempts', 'timing_complete', 'category_ms', 'error_code'])
    chart(output / 'latency.svg', paired)
    return result


def real(root, output, pairs, prices, ledger):
    from m12_bundle.budget import BudgetAdapter
    from m12_bundle.cost import costs
    before = shared_signature()
    groups, requests, tasks, calls = [], [], [], []
    adapter = BudgetAdapter(ledger)
    # One queue at a time; the only experimental factor is saved batch size.
    for size in [1, 4]:
        print(f'M12 real: index confirmation batch={size} tasks={pairs}', flush=True)
        group, rq, ts, cs = run_group(root, output / f'independent_c1_b{size}', n=pairs, concurrency=1,
                                     size=size, workload='independent', adapter=adapter, real=True)
        groups.append(group); requests.extend(rq); tasks.extend(ts); calls.extend(cs)
        if not group['passed'] or ledger.blocked:
            break
    after = shared_signature()
    stats = summarize_groups(requests, tasks, calls, groups)
    this_run = [r for r in ledger.rows if r['run_id'] == ledger.run_id]
    result = {'passed': len(groups) == 2 and all(g['passed'] for g in groups) and before == after and not ledger.blocked,
              'planned_tasks': pairs * 2, 'observed_unique_tasks': len(tasks), 'groups': stats,
              'comparison': comparison(stats), 'usage': usage_summary(calls), 'cost': costs(calls, prices),
              'sdk_attempt_ledger_entries': len(this_run), 'ledger_usage_complete': not ledger.blocked,
              'shared_rows_unchanged': before == after, 'external_model_calls': len(this_run),
              'model_scope': 'Real EMBEDDING indexing calls only; LLM/VISION extraction and QA generation are not measured.',
              'latency_scope': 'Task submission through ready index, using TestClient + same pipeline/DB + single test worker; excludes production supervisor startup.',
              'measurement_complete': len(tasks) == pairs * 2 and len(groups) == 2 and all(g['passed'] for g in groups),
              'real_model_quality_evaluation': 'not_executed', 'price_snapshot': prices}
    write(output / 'requests.json', requests); write(output / 'tasks.json', tasks); write(output / 'calls.json', calls)
    write(output / 'sdk_attempts.json', this_run); write(output / 'summary.json', result)
    csv_file(output / 'calls.csv', result['usage']['records'], ['group', 'id', 'task_id', 'attempt', 'prefix', 'model',
             'status', 'input_tokens', 'output_tokens', 'total_tokens', 'usage_known', 'input_source', 'other_usage', 'error_code'])
    return result
