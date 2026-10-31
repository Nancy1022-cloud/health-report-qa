"""Test real-worker-style lease renewal, single-item isolation, and retained errors."""
import copy
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from m13_bundle import recovery_support as support


class RecoverySupportChecks(unittest.TestCase):
    def test_normal_execution_keeps_original_token_and_budget(self):
        observed, event = [], threading.Event()
        task = {'id': 'test_task', 'execution_token': 'original_token', 'max_calls': 4, 'deadline': 'unchanged'}
        before = copy.deepcopy(task)
        def heartbeat(task_id, token):
            observed.append((task_id, token)); event.set(); return True
        repo = NS(policy=NS(heartbeat_seconds=.01), heartbeat=heartbeat)
        with support.renew_execution(repo, task):
            self.assertTrue(event.wait(2))
        self.assertEqual(task, before)
        self.assertEqual(observed[0], ('test_task', 'original_token'))
        self.assertFalse(any(t.name == 'm13-test-heartbeat-test_task' and t.is_alive() for t in threading.enumerate()))

    def test_a_fenced_token_does_not_get_replaced_or_forced_valid(self):
        observed, event = [], threading.Event()
        def heartbeat(task_id, token):
            observed.append((task_id, token)); event.set(); return False
        repo = NS(policy=NS(heartbeat_seconds=.01), heartbeat=heartbeat)
        with patch.object(support, 'append_diagnostic') as log:
            with support.renew_execution(repo, {'id': 'old', 'execution_token': 'stale'}): self.assertTrue(event.wait(2))
            self.assertEqual(log.call_args.args[0]['event'], 'heartbeat_fenced')
        self.assertEqual(observed, [('old', 'stale')])

    def test_heartbeat_storage_failure_is_not_silenced(self):
        event = threading.Event()
        def heartbeat(*args): event.set(); raise OSError('synthetic storage failure')
        repo = NS(policy=NS(heartbeat_seconds=.01), heartbeat=heartbeat)
        with patch.object(support, 'append_diagnostic'):
            with self.assertRaisesRegex(RuntimeError, 'heartbeat failed'):
                with support.renew_execution(repo, {'id': 'task', 'execution_token': 'token'}): self.assertTrue(event.wait(2))

    def test_original_business_exception_is_not_converted_into_success(self):
        repo = NS(policy=NS(heartbeat_seconds=.01), heartbeat=lambda *args: True)
        with self.assertRaisesRegex(ValueError, 'original failure'):
            with support.renew_execution(repo, {'id': 'task', 'execution_token': 'token'}): raise ValueError('original failure')

    def test_legacy_single_item_fixture_does_not_inherit_batch_four(self):
        @contextmanager
        def fake_isolated(root, output): yield 'connection', {'temporary_schema_cleaned': True}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'M12_EMBEDDING_BATCH_SIZE': '4'}), \
             patch.object(support, 'original_isolated', side_effect=fake_isolated), patch.object(support, 'export_records') as export:
            with support.isolated(Path(folder), Path(folder)):
                self.assertEqual(os.environ['M12_EMBEDDING_BATCH_SIZE'], '1')
            self.assertEqual(os.environ['M12_EMBEDDING_BATCH_SIZE'], '4')
            export.assert_called_once()
            config = json.loads((Path(folder) / 'configuration.json').read_text())
            self.assertEqual(config['inherited_batch_size'], '4')
            self.assertFalse(config['assertions_weakened'])

    def test_diagnostics_are_exported_even_on_probe_failure(self):
        @contextmanager
        def fake_isolated(root, output): yield 'connection', {}
        with tempfile.TemporaryDirectory() as folder, patch.object(support, 'original_isolated', side_effect=fake_isolated), \
             patch.object(support, 'export_records') as export:
            with self.assertRaises(AssertionError):
                with support.isolated(Path(folder), Path(folder)): raise AssertionError('failed check')
            export.assert_called_once()

    def test_append_retains_type_and_stack_for_the_failed_task(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'M13_RECOVERY_OUTPUT': folder}):
            support.append_diagnostic({'event': 'write_exception', 'task_id': 'synthetic', 'error_type': 'ValueError', 'traceback': 'synthetic stack'})
            row = json.loads((Path(folder) / 'pipeline_diagnostics.jsonl').read_text())
            self.assertEqual(row['task_id'], 'synthetic'); self.assertEqual(row['traceback'], 'synthetic stack')


if __name__ == '__main__': unittest.main()
