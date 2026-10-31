"""Isolated PostgreSQL schemas and synthetic files for M12 experiments."""

import copy
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from m12_bundle.analysis import write

SOURCE = '空腹血糖：5.7 mmol/L；参考范围：3.9-6.1。'


def fixture(number):
    raw = f'【合成体检报告】\n样本编号：{number:04d}；仅用于软件测试。\n{SOURCE}\n'
    raw += '\n'.join(f'第{i:02d}条记录：按原件保留指标、单位、检查日期与来源；样本{number:04d}。' for i in range(18))
    return {'title': f'M12合成报告{number:04d}', 'report_date': '2026-10-31', 'report_type': '体检报告',
            'raw_text': raw, 'metrics': [{'metric_code': 'fasting_glucose', 'metric_name': '空腹血糖', 'value': '5.7',
            'unit': 'mmol/L', 'reference_range': '3.9-6.1', 'source_text': SOURCE}], 'reviewed': True}


def vector_for(text, dimensions):
    digest = hashlib.sha256(text.encode()).digest()
    return [(digest[i % len(digest)] + 1) / 256. for i in range(dimensions)]


class ControlledAdapter:
    def embedding(self, content, timeout):
        result, usage = self.embeddings([content], timeout)
        return {'vector': result['vectors'][0]}, usage

    def embeddings(self, contents, timeout):
        dim = int(os.getenv('EMBEDDING_DIMENSIONS', '1024'))
        tokens = sum(max(1, len(t.encode()) // 3) for t in contents)
        return {'vectors': [vector_for(t, dim) for t in contents]}, {'prompt_tokens': tokens, 'total_tokens': tokens}


@contextmanager
def controlled_http(delay=.02):
    lock = threading.Lock()
    evidence = {'requests': 0, 'inputs': 0, 'provider': 'local_controlled_http', 'per_request_delay_s': delay}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            if not self.path.endswith('/embeddings'):
                self.send_error(404)
                return
            contents = request['input']
            contents = [contents] if isinstance(contents, str) else contents
            dimensions = request['dimensions']
            with lock:
                evidence['requests'] += 1
                evidence['inputs'] += len(contents)
            time.sleep(delay)
            tokens = sum(max(1, len(t.encode()) // 3) for t in contents)
            # Reverse transport order deliberately; clients must use each item's index.
            response = {'object': 'list', 'model': request['model'],
                        'data': [{'object': 'embedding', 'index': i, 'embedding': vector_for(t, dimensions)}
                                 for i, t in reversed(list(enumerate(contents)))],
                        'usage': {'prompt_tokens': tokens, 'total_tokens': tokens}}
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        # Local controlled interfaces must not inherit workstation SOCKS/HTTP proxies.
        clean = {key: '' for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']}
        clean.update(NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
        with patch.dict(os.environ, clean):
            yield f'http://127.0.0.1:{server.server_port}/v1', evidence
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def shared_signature():
    from app.db import connect_db
    from psycopg import sql
    tables = ['family_members', 'reports', 'report_chunks', 'metrics', 'query_logs',
              'source_uploads', 'report_revisions', 'revision_sources', 'idempotency_records']
    result = {}
    with connect_db() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        schema = conn.execute('SELECT current_schema()').fetchone()[0]
        for name in tables:
            if conn.execute('SELECT to_regclass(%s)', (name,)).fetchone()[0] is None:
                continue
            statement = sql.SQL("SELECT count(*),md5(COALESCE(string_agg(md5(row_to_json(t)::text),'' ORDER BY md5(row_to_json(t)::text)),'')) FROM {}.{} t").format(sql.Identifier(schema), sql.Identifier(name))
            count, digest = conn.execute(statement).fetchone()
            result[name] = {'count': count, 'content_md5': digest}
    return result


@contextmanager
def isolated(root, output, *, endpoint=None):
    from app.db import connect_db
    from app import migrate_m3
    from app.migrate_m10 import migrate as m10
    from app.migrate_m11 import migrate as m11
    from psycopg import sql
    base = connect_db
    schema = 'm11_m12_' + uuid4().hex
    result = {'schema': schema, 'temporary_schema_cleaned': False, 'data_source': 'fixed_synthetic'}
    created = False
    def connect():
        conn = base()
        try:
            conn.execute(sql.SQL('SET search_path TO {},public').format(sql.Identifier(schema)))
            return conn
        except BaseException:
            conn.close()
            raise
    try:
        with base() as conn:
            row = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
            if row is None:
                raise RuntimeError('The existing database must already have pgvector; no shared extension changes are made.')
            result['postgresql'] = conn.execute('SHOW server_version').fetchone()[0]
            result['pgvector'] = row[0]
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        created = True
        with tempfile.TemporaryDirectory(prefix='health_m12_') as folder, ExitStack() as stack:
            settings = {'UPLOAD_DIR': folder, 'M11_DB_SCHEMA': schema, 'M12_EMBEDDING_BATCH_SIZE': '1'}
            if endpoint is not None:
                for prefix in ['LLM', 'VISION', 'EMBEDDING']:
                    settings.update({prefix + '_MODEL': 'controlled-m12', prefix + '_BASE_URL': endpoint,
                                     prefix + '_API_KEY': 'synthetic-no-secret'})
            stack.enter_context(patch.dict(os.environ, settings))
            with connect() as conn:
                conn.execute((root / 'app/schema.sql').read_text(encoding='utf-8-sig'))
            with patch.object(migrate_m3, 'connect_db', side_effect=connect):
                migrate_m3.migrate()
            m10(connect)
            m11(connect)
            with connect() as conn:
                conn.execute("INSERT INTO family_members(id,name) VALUES ('a','M12合成甲'),('b','M12合成乙')")
            try:
                yield connect, result
            finally:
                # Preserve raw call evidence even when an experiment aborts before analysis.
                from psycopg.rows import dict_row
                try:
                    with connect() as conn:
                        conn.row_factory = dict_row
                        raw_calls = conn.execute('SELECT * FROM processing_calls ORDER BY created_at').fetchall()
                        raw_events = conn.execute('SELECT * FROM processing_events ORDER BY id').fetchall()
                        raw_tasks = conn.execute('SELECT id,member_id,report_id,kind,status,attempts,calls_reserved,created_at,finished_at,error_code,payload FROM processing_tasks ORDER BY created_at').fetchall()
                    write(output / 'raw_database_records.json', {'calls': raw_calls, 'events': raw_events, 'tasks': raw_tasks})
                except Exception as error:
                    result['raw_export_error_type'] = type(error).__name__
    finally:
        if created:
            try:
                with base() as conn:
                    conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
                result['temporary_schema_cleaned'] = True
            except Exception as error:
                result['cleanup_error_type'] = type(error).__name__
        write(output / 'environment.json', result)


def seed(service, number):
    from app.report_extract import normalize_fields
    from app.task_files import TaskFiles
    from PIL import Image
    body = fixture(number)
    image = io.BytesIO()
    Image.new('RGB', (16, 16), (number % 255, number // 255 % 255, 5)).save(image, format='PNG')
    files = TaskFiles(service.reports.drafts)
    metadata = files.stage('upload_' + uuid4().hex, 'a', f'synthetic_{number}.png', image.getvalue(), '.png')
    normalized = normalize_fields(body, body['raw_text'])
    draft = files.publish(metadata, {**normalized, 'model_info': {'model': 'synthetic-fixture', 'usage': None}})
    original = service.reports.drafts.path(draft['id']) / 'source.png'
    with service.repository.connection() as conn:
        service.repository.governance.register_source(conn, draft, service.reports.drafts.relative_original(original))
    return draft['id'], body


def service_for(connect, policy=None):
    from app.repositories.tasks import TaskRepository
    from app.task_worker import report_service
    from app.services.tasks import TaskService
    repository = TaskRepository(connect, policy)
    return TaskService(repository, report_service(repository))


class Worker:
    """One in-process test worker. Production subprocess supervision is checked separately."""
    def __init__(self, service, adapter=None):
        self.service, self.adapter = service, adapter
        self.stopping, self.failure = threading.Event(), None
        self.worker = threading.Thread(target=self.run, daemon=True)

    def run(self):
        from app.task_pipeline import Pipeline
        repo = self.service.repository
        identity = uuid4().hex
        try:
            with repo.singleton(identity):
                while not self.stopping.is_set():
                    repo.worker_heartbeat(identity)
                    task = repo.claim()
                    if task is None:
                        self.stopping.wait(.02)
                        continue
                    done = threading.Event()
                    def heartbeat():
                        while not done.wait(.5):
                            repo.heartbeat(task['id'], task['execution_token'])
                            repo.worker_heartbeat(identity)
                    heart = threading.Thread(target=heartbeat, daemon=True)
                    heart.start()
                    try:
                        Pipeline(repo, self.service.reports, self.adapter).execute(task)
                    finally:
                        done.set()
                        heart.join(timeout=5)
        except BaseException as error:
            self.failure = type(error).__name__
            self.stopping.set()

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *args):
        self.stopping.set()
        self.worker.join(timeout=70)
        if self.worker.is_alive():
            raise RuntimeError('Test worker did not stop; keep the environment.json for diagnosis.')
        if self.failure:
            raise RuntimeError('Test worker failed: ' + self.failure)
