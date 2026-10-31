"""安装器检查：只在临时目录操作小型自建 fixture。"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

class InstallerChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'project';self.bundle=Path(self.temp.name)/'bundle'
        self.root.mkdir();self.bundle.mkdir()
        spec=importlib.util.spec_from_file_location('health_m9_installer_under_test',Path(__file__).resolve().parents[1]/'run_m9.py')
        self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        self.m.ROOT=self.root;self.m.BUNDLE=self.bundle;self.m.EVIDENCE=self.root/'app/data/m9/v1'
        self.put(self.root/'compose.yaml','services: {}\n')
        self.put(self.root/'app/db.py','MODE = "old"\n')
        self.put(self.root/'app/keep.py','UNCHANGED = True\n')
        self.put(self.root/'.env','PRIVATE_SENTINEL\n')
        self.put(self.bundle/'payload/app/db.py','MODE = "new"\n')
        self.put(self.bundle/'payload/app/new.py','ADDED = True\n')
        self.m.write(self.root/'app/data/m8/v1/comparison.json',{'passed':True})
        self.manifest={'base_files':{p:self.m.digest(self.root/p) for p in ['compose.yaml','app/db.py','app/keep.py']},
                       'payload':{p:self.m.digest(self.bundle/'payload'/p) for p in ['app/db.py','app/new.py']},'harness':{}}
        self.m.write(self.bundle/'manifest.json',self.manifest)
    def put(self,path,content):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content,encoding='utf-8')
    def test_preflight_identifies_m8(self):self.assertEqual(self.m.inspect_source()['state'],'m8_ready')
    def test_apply_and_repeat(self):
        self.assertTrue(self.m.apply()['applied']);self.assertEqual(self.m.inspect_source()['state'],'applied')
        self.assertTrue(self.m.apply()['already_applied'])
    def test_unknown_source_change_blocks_apply(self):
        self.put(self.root/'app/db.py','USER_CHANGED = True\n')
        with self.assertRaises(RuntimeError):self.m.apply()
        self.assertIn('USER_CHANGED',(self.root/'app/db.py').read_text());self.assertFalse((self.root/'app/new.py').exists())
    def test_unknown_new_file_blocks_apply(self):
        self.put(self.root/'app/new.py','USER_ADDED = True\n')
        with self.assertRaises(RuntimeError):self.m.apply()
    def test_package_corruption_blocks_apply(self):
        self.put(self.bundle/'payload/app/new.py','CORRUPTED = True\n')
        with self.assertRaises(RuntimeError):self.m.apply()
    def test_windows_line_endings_and_bom_are_supported(self):
        (self.root/'app/db.py').write_bytes(b'\xef\xbb\xbfMODE = "old"\r\n')
        self.assertEqual(self.m.inspect_source()['state'],'m8_ready');self.m.apply()
    def test_missing_m8_gate_blocks_apply(self):
        self.m.write(self.root/'app/data/m8/v1/comparison.json',{'passed':False})
        with self.assertRaises(RuntimeError):self.m.apply()
    def test_rollback_restores_exact_bytes(self):
        original=b'\xef\xbb\xbfMODE = "old"\r\n';(self.root/'app/db.py').write_bytes(original)
        result=self.m.apply();self.m.rollback(result['backup'])
        self.assertEqual((self.root/'app/db.py').read_bytes(),original)
        self.assertFalse((self.root/'app/new.py').exists());self.assertEqual((self.root/'.env').read_text(),'PRIVATE_SENTINEL\n')
    def test_rollback_refuses_later_edit_before_any_mutation(self):
        result=self.m.apply();self.put(self.root/'app/db.py','USER_EDIT = True\n')
        with self.assertRaises(RuntimeError):self.m.rollback(result['backup'])
        self.assertTrue((self.root/'app/new.py').exists())
    def test_partial_write_is_restored(self):
        actual=self.m.os.replace;calls=0
        def fail_second(a,b):
            nonlocal calls;calls+=1
            if calls==2:raise OSError('controlled install interruption')
            return actual(a,b)
        with patch.object(self.m.os,'replace',side_effect=fail_second):
            with self.assertRaises(OSError):self.m.apply()
        self.assertEqual((self.root/'app/db.py').read_text(),'MODE = "old"\n');self.assertFalse((self.root/'app/new.py').exists())
    def test_snapshots_exclude_env_and_data(self):
        self.m.apply();files=self.m.source_manifest()
        self.assertNotIn('.env',files);self.assertFalse(any('app/data/' in p for p in files))
    def test_manual_flag_cannot_bypass_technical_gate(self):
        self.m.apply();result=self.m.finalize(True)
        self.assertFalse(result['passed']);self.assertFalse(result['technical_gate_passed'])
    def test_successful_v0_cannot_be_overwritten(self):
        self.m.apply();self.m.write(self.m.EVIDENCE/'checks/summary.json',{'passed':True,'source_manifest':self.m.source_manifest()})
        self.m.write(self.m.EVIDENCE/'V0/summary.json',{'passed':True})
        with self.assertRaisesRegex(RuntimeError,'已有成功 V0'):self.m.baseline(3)
    def test_path_traversal_is_blocked(self):
        with self.assertRaises(RuntimeError):self.m.target('../outside')
if __name__=='__main__':unittest.main()
