"""Repeatable upload -> cited QA -> revision demonstration in an isolated schema."""
import argparse
import copy
import io
import json
import re
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path.cwd().resolve()
sys.path.insert(0, str(ROOT))


def execute(output):
    from fastapi.testclient import TestClient
    from PIL import Image, ImageDraw
    from m12_bundle.analysis import write
    from m12_bundle.environment import isolated, service_for, ControlledAdapter
    from m12_bundle.experiments import application
    from m11_bundle.tests.support import BODY
    from app import rag, retrieval
    from app.dependencies import get_report_service, get_query_service
    from app.services.queries import QueryService
    from app.query_routes import router as query_router
    from app.upload_routes import router as upload_router
    from app.task_pipeline import Pipeline
    from app.task_policy import TaskPolicy
    records, environment, failure = [], None, None
    def record(name, passed, **detail):
        records.append({'name': name, 'passed': bool(passed), **detail})
        write(output / 'checks.json', records)
        if not passed:
            raise AssertionError(name)
    class Adapter(ControlledAdapter):
        def fields(self, prefix, content, timeout):
            return {'fields': copy.deepcopy({k: v for k, v in BODY.items() if k != 'reviewed'}),
                    'model': 'm13-controlled-extraction'}, None
    class Generation:
        def __enter__(self):
            return NS(chat=NS(completions=NS(create=self.create)))
        def __exit__(self, *args):
            pass
        def create(self, **kwargs):
            evidence = json.loads(kwargs['messages'][-1]['content'])['evidence']
            item = next(e for e in evidence if 'mmol/L' in e['content'])
            value = re.search(r'(\d+\.\d+)\s*mmol/L', item['content'])[1]
            data = {'answerable': True, 'answer': f'报告记载空腹血糖为 {value} mmol/L [[{item["source_id"]}]]',
                    'source_ids': [item['source_id']]}
            return NS(usage=None, choices=[NS(finish_reason='stop', message=NS(content=json.dumps(data, ensure_ascii=False)))])
    try:
        with isolated(ROOT, output, endpoint='http://127.0.0.1:9/v1') as (connect, environment), ExitStack() as stack:
            service = service_for(connect, TaskPolicy(min_call_interval=0))
            repo = service.repository
            app = application(service)
            app.include_router(query_router)
            app.include_router(upload_router)
            app.dependency_overrides[get_report_service] = lambda: service.reports
            app.dependency_overrides[get_query_service] = lambda: QueryService(
                rag.run_query, None, repo.governance.version_snapshot, repo.governance.invalidate_query)
            for module in [rag, retrieval]:
                stack.enter_context(patch.object(module, 'connect_db', side_effect=connect))
            stack.enter_context(patch.object(rag, 'make_client', side_effect=lambda prefix: Generation()))
            def complete(task_id):
                task = repo.claim()
                record('claimed_requested_task', task is not None and task['id'] == task_id)
                Pipeline(repo, service.reports, Adapter()).execute(task)
                result = repo.get(task_id, 'a')
                record('task_completed', result['status'] == 'succeeded', kind=result['kind'])
                return result
            image = Image.new('RGB', (760, 180), 'white')
            ImageDraw.Draw(image).multiline_text((20, 25), 'SYNTHETIC SOFTWARE TEST REPORT\nFasting glucose: 5.7 mmol/L\nReference: 3.9-6.1; date: 2026-10-31', fill='black', spacing=12)
            data = io.BytesIO(); image.save(data, format='PNG')
            output.mkdir(parents=True, exist_ok=True)
            (output / 'synthetic_report.png').write_bytes(data.getvalue())
            with TestClient(app) as client:
                submitted = client.post('/api/tasks/upload', data={'member_id': 'a'},
                                        files={'file': ('m13_synthetic.png', data.getvalue(), 'image/png')},
                                        headers={'Idempotency-Key': 'm13-upload'})
                record('multipart_upload_accepted', submitted.status_code == 202)
                upload = submitted.json(); rid = upload['report_id']
                extracted = complete(upload['id'])
                write(output / 'upload.json', {'response': upload, 'completed': extracted})
                review = copy.deepcopy(BODY)
                confirmed = client.post(f'/api/tasks/reports/{rid}/confirm?member_id=a', json=review,
                                        headers={'Idempotency-Key': 'm13-confirm'})
                record('confirm_accepted', confirmed.status_code == 202)
                done = complete(confirmed.json()['id'])
                repeated = client.post(f'/api/tasks/reports/{rid}/confirm?member_id=a', json=review,
                                       headers={'Idempotency-Key': 'm13-confirm'})
                record('duplicate_confirmation_reuses_task', repeated.status_code == 202 and repeated.json()['id'] == done['id'])
                query = {'question': '空腹血糖是多少？', 'member_id': 'a', 'report_ids': [rid],
                         'retrieval_mode': 'keyword', 'chunk_version': 'fixed200o40', 'with_answer': True}
                first = client.post('/api/query', json=query)
                answer = first.json()
                record('original_cited_answer', first.status_code == 200 and answer['status'] == 'answered'
                       and '5.7' in answer['answer'] and bool(answer['citations']))
                old_source = answer['citations'][0]['source_id']
                write(output / 'question_before.json', {'request': query, 'response': answer})
                review['raw_text'] = review['raw_text'].replace('5.7', '5.8')
                review['metrics'][0]['value'] = '5.8'
                review['metrics'][0]['source_text'] = review['metrics'][0]['source_text'].replace('5.7', '5.8')
                revision = client.post(f'/api/tasks/reports/{rid}/revisions?member_id=a',
                                       json={'expected_version': 1, 'reason': 'synthetic M13 correction', 'review': review},
                                       headers={'Idempotency-Key': 'm13-revise'})
                record('revision_accepted', revision.status_code == 202)
                complete(revision.json()['id'])
                second = client.post('/api/query', json=query)
                after = second.json()
                record('revised_answer_uses_current_evidence', second.status_code == 200 and after['status'] == 'answered'
                       and '5.8' in after['answer'] and all(c['source_id'] != old_source for c in after['citations']))
                provenance = service.reports.repository.provenance(rid, 'a')
                old = service.reports.repository.resolve_source(old_source, 'a')
                record('original_and_two_versions_traceable', provenance['current_version'] == provenance['index_version'] == 2
                       and len(provenance['revisions']) == 2 and provenance['original_extraction_available'] is True)
                record('old_source_identified_as_stale', old['is_current'] is False)
                denied = client.get(f'/api/tasks/{done["id"]}?member_id=b')
                record('other_member_cannot_read_task', denied.status_code == 404)
                with repo.connection() as conn:
                    counts = {t: conn.execute(f'SELECT count(*) AS n FROM {t}').fetchone()['n']
                              for t in ['reports', 'metrics', 'report_revisions']}
                record('unique_current_report_and_metric', counts == {'reports': 1, 'metrics': 1, 'report_revisions': 2}, counts=counts)
                write(output / 'question_after.json', {'request': query, 'response': after})
                write(output / 'provenance.json', provenance)
                write(output / 'old_source.json', old)
    except Exception as error:
        failure = {'type': type(error).__name__, 'message': str(error)[:500]}
    result = {'passed': failure is None and bool(records) and all(r['passed'] for r in records)
              and bool(environment and environment.get('temporary_schema_cleaned')),
              'checks': records, 'failure': failure, 'temporary_schema_cleaned': bool(environment and environment.get('temporary_schema_cleaned')),
              'external_model_calls': 0, 'model_source': 'controlled extraction and generation; real PostgreSQL, routes, indexing, retrieval and citation validation',
              'scope': 'Synthetic repeatable demo; not a real-model answer-quality evaluation'}
    write(output / 'summary.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    result = execute(parser.parse_args().output.resolve())
    print(json.dumps({k: v for k, v in result.items() if k != 'checks'}, ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)
