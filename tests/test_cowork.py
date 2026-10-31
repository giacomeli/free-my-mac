import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class CoworkTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('cowork_maintenance'))
        import cowork_maintenance
        return cowork_maintenance

    def setup_home(self, root):
        base = root / 'Library/Application Support/Claude'
        base.mkdir(parents=True)
        config = base / 'claude_desktop_config.json'
        config.write_text(json.dumps({'mcpServers': {'fixture': {'command':'test'}}, 'preferences':{'other':True}}))
        (base / 'vm_bundles').mkdir()
        (base / 'vm_bundles/disk').write_text('fixture')
        (base / 'local-agent-mode-sessions').mkdir()
        (base / 'local-agent-mode-sessions/session').write_text('keep')
        return base, config

    def test_dry_run_does_not_modify_anything(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as d:
            base, config = self.setup_home(Path(d)); before = config.read_bytes()
            m.disable(Path(d), apply=False)
            self.assertEqual(before, config.read_bytes())
            self.assertTrue((base / 'vm_bundles/disk').exists())

    def test_apply_backup_preserves_other_preferences_and_sessions(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as d, patch.object(m, 'assert_closed'):
            root = Path(d);base, config = self.setup_home(root);before=config.read_bytes()
            result=m.disable(root, apply=True)
            self.assertEqual(Path(result['backup']).read_bytes(),before)
            data=json.loads(config.read_text())
            self.assertIs(data['preferences']['secureVmFeaturesEnabled'],False)
            self.assertTrue(data['preferences']['other'])
            self.assertIn('fixture',data['mcpServers'])
            self.assertFalse((base/'vm_bundles').exists())
            self.assertTrue((base/'local-agent-mode-sessions/session').exists())
            self.assertEqual(m.disable(root,apply=True)['state'],'já aplicado')

    def test_running_app_blocks_before_writes(self):
        m=self.module()
        with tempfile.TemporaryDirectory() as d, patch.object(m,'assert_closed',side_effect=ValueError('feche o app')):
            root=Path(d);base,config=self.setup_home(root);before=config.read_bytes()
            with self.assertRaises(ValueError):m.disable(root,apply=True)
            self.assertEqual(before,config.read_bytes())

    def test_symlink_vm_and_malformed_config_rejected(self):
        m=self.module()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);base,config=self.setup_home(root)
            (base/'vm_bundles/link').symlink_to(config)
            with patch.object(m,'assert_closed'),self.assertRaises(ValueError):m.disable(root,apply=True)
            config.write_text('{broken')
            with self.assertRaises(ValueError):m.disable(root,apply=False)
