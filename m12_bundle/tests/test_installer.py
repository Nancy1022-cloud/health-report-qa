"""Installer and completion gates use explicitly synthetic prior-evidence fixtures."""

import importlib.util
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from m12_bundle import run_m12 as runner


class InstallerChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        original_root = runner.ROOT
        spec = json.loads((runner.BUNDLE / 'manifest.json').read_text())
        for relative, expected in spec['base_files'].items():
            candidates = [original_root / 'm11_bundle/payload' / relative, original_root / relative]
            source = next(p for p in candidates if p.is_file() and runner.digest(p) == expected)
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        for relative in spec.get('prerequisite_files', {}):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original_root / relative, target)
        v0 = self.root / 'app/data/m9/v1/V0'
        for relative in ['summary.json', 'source_manifest.json', 'source_snapshot.zip', 'task_manifest.json', 'harness/tests/baseline_probe.py']:
            path = v0 / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'passed': True, 'synthetic_installer_fixture': True}), encoding='utf-8')
        for stage in [10, 11]:
            path = self.root / f'app/data/m{stage}/v1/summary.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'passed': True, 'synthetic_installer_fixture': True}))
        for relative in ['run_m9.py', 'runtime_inventory.py', 'manifest.json']:
            path = v0 / 'harness' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# synthetic installer fixture\n')
        hashes = {relative: runner.digest(v0 / 'harness' / relative, True) for relative in
                  ['run_m9.py', 'runtime_inventory.py', 'tests/baseline_probe.py', 'manifest.json']}
        (v0 / 'harness_manifest.json').write_text(json.dumps(hashes))
        chain = {'M9_V0': {relative: runner.digest(v0 / relative, True) for relative in
                          ['summary.json', 'source_manifest.json', 'source_snapshot.zip', 'task_manifest.json']},
                 'M10_summary': runner.digest(self.root / 'app/data/m10/v1/summary.json', True)}
        (self.root / 'app/data/m11/v1/v0_preservation.json').write_text(json.dumps(chain))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(runner, 'ROOT', self.root))
        self.stack.enter_context(patch.object(runner, 'EVIDENCE', self.root / 'app/data/m12/v1'))

    def test_preflight_recognizes_m11(self):
        self.assertEqual(runner.preflight()['state'], 'm11_ready')

    def test_apply_and_reapply_keep_first_backup(self):
        first = runner.apply()
        second = runner.apply()
        self.assertTrue(second['already_applied'])
        self.assertEqual(len(list((self.root / 'backups').iterdir())), 1)
        self.assertTrue((self.root / first['backup'] / 'rollback.json').exists())

    def test_unknown_source_is_never_overwritten(self):
        path = self.root / 'app/main.py'
        path.write_text('user_edit = True\n')
        with self.assertRaises(RuntimeError): runner.apply()
        self.assertEqual(path.read_text(), 'user_edit = True\n')

    def test_original_evidence_is_unchanged(self):
        before = runner.protected_signature()
        runner.apply()
        self.assertEqual(before, runner.protected_signature())

    def test_missing_v0_blocks_install(self):
        (self.root / 'app/data/m9/v1/V0/source_snapshot.zip').unlink()
        with self.assertRaises(RuntimeError): runner.apply()

    def test_m11_original_signature_blocks_replaced_v0(self):
        (self.root / 'app/data/m9/v1/V0/task_manifest.json').write_text('{"replacement":true}')
        with self.assertRaises(RuntimeError): runner.apply()

    def test_changed_old_evidence_blocks_later_work(self):
        runner.apply()
        (self.root / 'app/data/m11/v1/summary.json').write_text('{"passed":true,"changed":true}')
        with self.assertRaises(RuntimeError): runner.require_applied()

    def test_atomic_restore_when_second_replace_fails(self):
        before = {p: runner.digest(self.root / p, True) for p in runner.manifest()['base_files']}
        original = runner.os.replace
        calls = 0
        def replace(a, b):
            nonlocal calls
            calls += 1
            if calls == 2: raise OSError('synthetic write failure')
            return original(a, b)
        with patch.object(runner.os, 'replace', side_effect=replace):
            with self.assertRaises(OSError): runner.apply()
        self.assertEqual(before, {p: runner.digest(self.root / p, True) for p in before})
        self.assertEqual(list(self.root.rglob('*.m12-install-tmp')), [])

    def test_rollback_rejects_later_edits(self):
        result = runner.apply()
        (self.root / 'app/main.py').write_text('later_edit=True')
        factory = MagicMock()
        factory.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (0,)
        with patch('app.db.connect_db', factory):
            with self.assertRaises(RuntimeError): runner.rollback(result['backup'])

    def test_rollback_stops_for_active_tasks(self):
        result = runner.apply()
        factory = MagicMock()
        factory.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (1,)
        with patch('app.db.connect_db', factory):
            with self.assertRaises(RuntimeError): runner.rollback(result['backup'])

    def gate(self, manual, real, skip=None):
        source = {'fixture': 'synthetic'}
        for name in ['checks', 'live', 'database', 'baseline_replay', 'load', 'operational', 'analysis']:
            value = {'passed': True, 'source_manifest': source}
            if name == 'load': value['full_size_complete'] = True
            runner.write(runner.EVIDENCE / name / 'summary.json', value)
        if real:
            runner.write(runner.EVIDENCE / 'real/summary.json', {'passed': True, 'observed_unique_tasks': 10, 'source_manifest': source})
        with patch.object(runner, 'require_check'), patch.object(runner, 'report', return_value={'passed': True}), patch.object(runner, 'source_manifest', return_value=source):
            return runner.finalize(manual, skip)

    def test_manual_flag_cannot_replace_real_measurement_or_explicit_gap(self):
        result = self.gate(True, False)
        self.assertFalse(result['passed'])

    def test_full_scope_requires_real_task_records(self):
        result = self.gate(True, True)
        self.assertTrue(result['passed']); self.assertTrue(result['full_scope_completed'])

    def test_skipped_real_is_labeled_partial_scope(self):
        result = self.gate(True, False, '真实接口额度不足，未完成模型测量')
        self.assertTrue(result['passed']); self.assertFalse(result['full_scope_completed'])
        self.assertFalse(result['real_measurement_completed'])

    def test_manual_not_done_is_draft(self):
        result = self.gate(False, True)
        self.assertFalse(result['passed']); self.assertTrue(result['technical_gate_passed'])


if __name__ == '__main__':
    unittest.main()
