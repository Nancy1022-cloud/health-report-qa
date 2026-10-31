"""M13-only adapter around the preserved M11 recovery probe. No application edits."""
import json
import os
import threading
import traceback
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.task_pipeline import Pipeline
from m11_bundle.tests.support import isolated as original_isolated, write

_log_lock = threading.Lock()


def append_diagnostic(value):
    folder = os.getenv('M13_RECOVERY_OUTPUT')
    if not folder:
        return
    path = Path(folder) / 'pipeline_diagnostics.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with _log_lock, path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, default=str) + '\n')


@contextmanager
def renew_execution(repository, task):
    """Use the same token/deadline-checked heartbeat as the production supervisor."""
    done = threading.Event()
    errors = []
    def renew():
        while not done.wait(repository.policy.heartbeat_seconds):
            try:
                if not repository.heartbeat(task['id'], task['execution_token']):
                    append_diagnostic({'event': 'heartbeat_fenced', 'task_id': task['id']})
                    return
            except Exception as error:
                errors.append(type(error).__name__)
                append_diagnostic({'event': 'heartbeat_error', 'task_id': task['id'], 'error_type': type(error).__name__})
                return
    thread = threading.Thread(target=renew, name='m13-test-heartbeat-' + task['id'], daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(timeout=8)
        if thread.is_alive():
            raise RuntimeError('M13 test heartbeat did not stop')
        if errors:
            raise RuntimeError('M13 test heartbeat failed: ' + ','.join(errors))


class RecoveryPipeline(Pipeline):
    def execute(self, task):
        with renew_execution(self.repository, task):
            try:
                return super().execute(task)
            finally:
                try:
                    with self.repository.connection() as conn:
                        row = conn.execute('SELECT id,kind,status,stage,error_code,attempts,calls_reserved,lease_until,deadline, '
                                           "payload->>'embedding_batch_size' AS embedding_batch_size FROM processing_tasks WHERE id=%s",
                                           (task['id'],)).fetchone()
                    append_diagnostic({'event': 'pipeline_returned', 'task': dict(row) if row else None})
                except Exception as error:
                    append_diagnostic({'event': 'diagnostic_read_failed', 'task_id': task['id'], 'error_type': type(error).__name__})

    def write(self, context):
        try:
            return super().write(context)
        except Exception as error:
            append_diagnostic({'event': 'write_exception', 'task_id': context.task['id'],
                               'error_type': type(error).__name__, 'message': str(error)[:1000],
                               'traceback': traceback.format_exc()[-8000:]})
            raise

    def extract(self, context):
        try:
            return super().extract(context)
        except Exception as error:
            append_diagnostic({'event': 'extract_exception', 'task_id': context.task['id'],
                               'error_type': type(error).__name__, 'message': str(error)[:1000],
                               'traceback': traceback.format_exc()[-8000:]})
            raise


def export_records(connect, output):
    from psycopg.rows import dict_row
    with connect() as conn:
        conn.row_factory = dict_row
        tasks = conn.execute('SELECT id,report_id,kind,status,stage,error_code,attempts,calls_reserved,max_calls, '
                             "generation,lease_until,deadline,payload->>'embedding_batch_size' AS embedding_batch_size "
                             'FROM processing_tasks ORDER BY created_at').fetchall()
        calls = conn.execute('SELECT id,task_id,attempt,stage,prefix,status,usage,error_code FROM processing_calls ORDER BY created_at').fetchall()
        events = conn.execute('SELECT task_id,event,attempt,detail,created_at FROM processing_events ORDER BY id').fetchall()
    write(output / 'task_records.json', {'tasks': tasks, 'calls': calls, 'events': events,
                                       'data_source': 'isolated_synthetic', 'external_model_calls': 0})


@contextmanager
def isolated(root, output):
    # M11 assertions assume one embedding input per call. Batch-4 is verified separately.
    inherited = os.getenv('M12_EMBEDDING_BATCH_SIZE', '1')
    settings = {'M12_EMBEDDING_BATCH_SIZE': '1', 'M13_RECOVERY_OUTPUT': str(output)}
    with patch.dict(os.environ, settings), original_isolated(root, output) as (connect, environment):
        write(output / 'configuration.json', {'inherited_batch_size': inherited, 'test_batch_size': 1,
              'inline_execution_heartbeat': True, 'lease_seconds': 3, 'heartbeat_seconds': .5,
              'crash_child_heartbeat_stops_on_exit': True, 'assertions_weakened': False,
              'external_model_calls': 0})
        try:
            yield connect, environment
        finally:
            # Export before the original context removes its temporary schema.
            export_records(connect, output)
