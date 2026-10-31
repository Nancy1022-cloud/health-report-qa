"""Prevent false completion, source drift, unsafe file restore, and unblocked model calls."""
import io
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from m13_bundle import run_m13 as runner, storage


class CompletionChecks(unittest.TestCase):
    def test_ci_flag_cannot_hide_failed_engineering(self):
        result = runner.completion({'database': False}, True, True, True, None)
        self.assertFalse(result['passed']); self.assertFalse(result['full_scope_completed'])

    def test_missing_manual_review_is_incomplete(self):
        self.assertFalse(runner.completion({'checks': True}, False, True, True, None)['passed'])

    def test_missing_remote_ci_requires_explicit_gap(self):
        self.assertFalse(runner.completion({'checks': True}, True, False, True, None)['passed'])

    def test_ci_gap_can_only_complete_local_scope(self):
        result = runner.completion({'checks': True}, True, False, True, 'GitHub Actions unavailable')
        self.assertTrue(result['passed']); self.assertFalse(result['full_scope_completed'])

    def test_missing_real_measurement_remains_partial(self):
        result = runner.completion({'checks': True}, True, True, False, None)
        self.assertTrue(result['passed']); self.assertFalse(result['full_scope_completed'])

    def test_full_scope_needs_all_evidence(self):
        self.assertTrue(runner.completion({'checks': True}, True, True, True, None)['full_scope_completed'])


class EvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(runner, 'ROOT', self.root).start()
        patch.object(runner, 'EVIDENCE', self.root / 'app/data/m13/v1').start()

    def test_absolute_or_parent_escape_is_rejected(self):
        with self.assertRaises(RuntimeError): runner.relative_path('../outside')

    def test_normalized_source_digest_accepts_windows_line_endings(self):
        path = self.root / 'source.py'; path.write_bytes(b'x = 1\n'); before = runner.digest(path)
        path.write_bytes(b'\xef\xbb\xbfx = 1\r\n')
        self.assertEqual(before, runner.digest(path))

    def test_raw_evidence_signature_detects_even_line_ending_change(self):
        for n in range(9, 13): runner.write(self.root / f'app/data/m{n}/v1/summary.json', {'passed': True})
        before = runner.prior_signature()
        path = self.root / 'app/data/m12/v1/summary.json'
        path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.assertNotEqual(before, runner.prior_signature())

    def test_stale_check_is_rejected(self):
        runner.write(runner.EVIDENCE / 'checks/summary.json', {'passed': True, 'source_manifest': {'old': 'source'}})
        with self.assertRaises(RuntimeError): runner.require_result('checks')

    def test_prior_failure_cannot_be_overridden(self):
        for n in range(9, 13): runner.write(self.root / f'app/data/m{n}/v1/summary.json', {'passed': n != 11})
        with self.assertRaises(RuntimeError): runner.prior_signature()

    def test_preflight_does_not_rewrite_an_existing_evidence_lock(self):
        for n in range(9, 13): runner.write(self.root / f'app/data/m{n}/v1/summary.json', {'passed': True})
        runner.write(self.root / 'app/data/m12/v1/summary.json', {'passed': True, 'technical_gate_passed': True, 'source_manifest': {}})
        runner.write(runner.EVIDENCE / 'prior_evidence_signature.json', {'original': 'preserved'})
        from m12_bundle import run_m12
        with patch.object(runner, 'integrity'), patch.object(run_m12, 'require_applied'), patch.object(run_m12, 'source_manifest', return_value={}):
            with self.assertRaises(RuntimeError): runner.preflight()
        self.assertEqual(runner.read(runner.EVIDENCE / 'prior_evidence_signature.json'), {'original': 'preserved'})

    def test_a_new_backup_attempt_invalidates_old_success(self):
        for name in ['restore', 'restart', 'live']:
            runner.write(runner.EVIDENCE / name / 'summary.json', {'passed': True})
        with patch.object(runner, 'require_prior'): runner.start_backup()
        self.assertFalse(runner.read(runner.EVIDENCE / 'restore/summary.json')['passed'])

    def test_failed_live_request_cannot_reuse_a_previous_success(self):
        runner.write(runner.EVIDENCE / 'live/summary.json', {'passed': True})
        with patch.object(runner, 'require_prior'), patch.object(runner.urllib.request, 'urlopen', side_effect=OSError('offline')):
            with self.assertRaises(OSError): runner.live('http://127.0.0.1:1')
        self.assertFalse(runner.read(runner.EVIDENCE / 'live/summary.json')['passed'])

    def test_restart_allows_only_the_expected_heartbeat_change(self):
        before = {'database': {'tables': {'processing_worker_state': 'old', 'processing_tasks': 'same'}}, 'files': {}, 'active_tasks': 0}
        after = copy.deepcopy(before); after['database']['tables']['processing_worker_state'] = 'new'
        path = self.root / 'snapshot.json'; runner.write(path, before)
        with patch.object(storage, 'snapshot', return_value=after):
            self.assertTrue(runner.compare_snapshot(path)['passed'])

    def test_restart_detects_a_changed_task_even_if_heartbeat_changed(self):
        before = {'database': {'tables': {'processing_worker_state': 'old', 'processing_tasks': 'same'}}, 'files': {}, 'active_tasks': 0}
        after = copy.deepcopy(before); after['database']['tables'].update(processing_worker_state='new', processing_tasks='changed')
        path = self.root / 'snapshot.json'; runner.write(path, before)
        with patch.object(storage, 'snapshot', return_value=after):
            self.assertFalse(runner.compare_snapshot(path)['passed'])


class RemoteCIChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(runner, 'ROOT', self.root).start()
        patch.object(runner, 'EVIDENCE', self.root / 'app/data/m13/v1').start()
        patch.object(runner, 'require_prior').start()
        patch.object(runner, 'source_manifest', return_value={'code.py': 'expected'}).start()
        (self.root / '.git').mkdir()
        (self.root / '.git/config').write_text('[remote "origin"]\nurl = https://github.com/example/health.git\n')
        self.run = {'status': 'completed', 'conclusion': 'success', 'path': '.github/workflows/health-m13.yml', 'head_sha': 'a' * 40}
        self.artifact = self.root / 'artifact.zip'

    def imported(self, source=None):
        item = {'passed': True, 'database_checks_executed': True, 'external_model_calls': 0,
                'external_connections_blocked': True, 'github_run_id': '123', 'commit_sha': 'a' * 40,
                'source_manifest': source if source is not None else {'code.py': 'expected'}}
        with zipfile.ZipFile(self.artifact, 'w') as archive:
            archive.writestr('ci_summary.json', json.dumps(item))
        response = io.BytesIO(json.dumps(self.run).encode())
        with patch.object(runner.urllib.request, 'urlopen', return_value=response):
            return runner.import_ci(self.artifact, 'https://github.com/example/health/actions/runs/123')

    def test_real_run_status_and_matching_source_are_required(self):
        self.assertTrue(self.imported()['passed'])

    def test_failed_remote_run_cannot_be_overridden_by_artifact(self):
        self.run['conclusion'] = 'failure'
        self.assertFalse(self.imported()['passed'])

    def test_green_run_on_different_source_is_rejected(self):
        self.assertFalse(self.imported({'code.py': 'different'})['passed'])


class BackupChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.uploads = self.root / 'uploads'; self.uploads.mkdir()
        (self.uploads / 'source.bin').write_bytes(bytes(range(256)))

    def test_file_backup_restores_exact_binary_bytes(self):
        archive = self.root / 'uploads.zip'
        with patch.dict(os.environ, {'UPLOAD_DIR': str(self.uploads)}): storage.archive_uploads(archive)
        destination = self.root / 'restored/uploads'
        storage.restore_files(archive, destination)
        self.assertEqual(storage.file_manifest(self.uploads), storage.file_manifest(destination))

    def test_modified_upload_is_detected(self):
        before = storage.file_manifest(self.uploads)
        (self.uploads / 'source.bin').write_bytes(b'changed')
        self.assertNotEqual(before, storage.file_manifest(self.uploads))

    def test_backup_cannot_include_itself(self):
        with patch.dict(os.environ, {'UPLOAD_DIR': str(self.uploads)}):
            with self.assertRaises(RuntimeError): storage.archive_uploads(self.uploads / 'recursive.zip')

    def test_restore_cannot_overwrite_an_existing_directory(self):
        archive = self.root / 'empty.zip'
        with zipfile.ZipFile(archive, 'w'): pass
        with self.assertRaises(RuntimeError): storage.restore_files(archive, self.uploads)

    def test_restore_rejects_archive_escape(self):
        archive = self.root / 'bad.zip'
        with zipfile.ZipFile(archive, 'w') as out: out.writestr('../outside', 'bad')
        with self.assertRaises(RuntimeError): storage.restore_files(archive, self.root / 'restored')
        self.assertFalse((self.root / 'outside').exists())

    def test_file_symlinks_are_not_followed(self):
        try: (self.uploads / 'link').symlink_to(self.uploads / 'source.bin')
        except (OSError, NotImplementedError): self.skipTest('symlink creation unavailable')
        with self.assertRaises(RuntimeError): storage.file_manifest(self.uploads)


class NetworkChecks(unittest.TestCase):
    def test_network_guard_is_inherited_by_child_process(self):
        env = runner.subprocess_environment()
        env['DB_HOST'] = '127.0.0.1'
        script = "import socket; socket.socket().connect(('203.0.113.1',443))"
        result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('M13 controlled checks prohibit', result.stderr)


if __name__ == '__main__': unittest.main()
