import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dev_audit import Policy, Finding, identity, make_plan, execute_plan
from maintenance import file_action, command_catalog, docker_endpoint
from explicit_removal import allowed, validate, remove


class MaintenanceTests(unittest.TestCase):
    def test_build_requires_manifest_and_no_tracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            policy = Policy(home)
            target = home / 'Projects/demo/target'
            target.mkdir(parents=True)
            self.assertIsNone(file_action(target, policy))
            (target.parent / 'Cargo.toml').write_text('[package]')
            self.assertIsNotNone(file_action(target, policy))
            (target / 'sketchpad').mkdir()
            f = Finding('Builds', str(target), 'Cargo', '', action='delete-reviewed', identity=identity(target))
            plan = make_plan([f], policy)
            with self.assertRaises(ValueError):
                execute_plan(plan, policy)

    def test_installer_age_and_changed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            policy = Policy(home, days=60)
            p = home / 'Downloads/old.dmg'
            p.parent.mkdir()
            p.write_text('installer')
            self.assertIsNone(file_action(p, policy))
            os.utime(p, (1, 1))
            self.assertIsNotNone(file_action(p, policy))
            f = Finding('Instaladores', str(p), '', '', action='delete-reviewed', identity=identity(p))
            plan = make_plan([f], policy)
            self.assertIn('simulação', execute_plan(plan, policy)[0])
            p.write_text('changed')
            with self.assertRaises(ValueError):
                execute_plan(plan, policy)

    def test_arbitrary_path_and_symlink_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            policy = Policy(home)
            p = home / 'important'
            p.mkdir()
            self.assertIsNone(file_action(p, policy))
            (home / 'Library/Fonts').mkdir(parents=True)
            link = home / 'Library/Fonts/f.ttf'
            link.symlink_to(p)
            self.assertIsNone(file_action(link, policy))

    def test_native_catalog_never_prunes_volumes_or_loads_shell(self):
        with patch('maintenance.shutil.which', side_effect=lambda name: '/tools/' + name):
            actions = command_catalog()
        commands = [a['argv'] for a in actions]
        self.assertFalse(any('volume' in c or 'system' in c and 'prune' in c for c in commands))
        self.assertIn(['/tools/removemacai', 'off'], commands)
        self.assertFalse(any(c[0].endswith('/sh') for c in commands))

    def test_remote_docker_rejected(self):
        with patch.dict(os.environ, {'DOCKER_HOST': 'ssh://example.invalid'}):
            with self.assertRaises(ValueError):
                docker_endpoint('/tools/docker')

    def test_explicit_removal_rejects_system_roots_and_agent_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Policy(Path(tmp).resolve())
            for p in ('/System', '/opt', '/usr/local', '/System/Applications/Finder.app'):
                self.assertFalse(allowed(Path(p), policy))
            p = policy.home / 'Library/Application Support/Claude'
            p.mkdir(parents=True)
            self.assertFalse(allowed(p, policy))

    def test_explicit_removal_only_deletes_confirmed_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Policy(Path(tmp).resolve())
            p = policy.home / 'Library/Application Support/OldFixtureApp'
            p.mkdir(parents=True)
            (p / 'data').write_text('fixture')
            import subprocess
            with patch('explicit_removal.sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='REMOVER OldFixtureApp'), patch('explicit_removal.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', '')), patch('explicit_removal.Path.cwd', return_value=policy.home):
                remove(p, policy, True)
            self.assertFalse(p.exists())
            self.assertTrue(list(policy.home.glob('free-my-mac-remove-*.jsonl')))

    def test_explicit_nested_sketchpad_and_readonly_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Policy(Path(tmp).resolve())
            p = policy.home / 'Library/Application Support/OldFixtureApp'
            p.mkdir(parents=True)
            (p / 'sketchpad').mkdir()
            with self.assertRaises(ValueError):
                validate(p, policy)
            (p / 'sketchpad').rmdir()
            from types import SimpleNamespace
            with patch('explicit_removal.os.statvfs', return_value=SimpleNamespace(f_flag=1)):
                with self.assertRaises(ValueError):
                    validate(p, policy)

    def test_native_cache_protection_uses_configured_path(self):
        from maintenance import check_native_protections
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / 'custom-cache'
            p.mkdir()
            with patch('maintenance.run', return_value={'ok': True, 'output': str(p), 'error': ''}):
                with self.assertRaises(ValueError):
                    check_native_protections('npm-cache', ['npm'], [p])

    def test_native_probe_and_execution_share_cwd(self):
        from maintenance import check_native_protections
        with patch('maintenance.run', return_value={'ok': True, 'output': '/nonexistent/cache', 'error': ''}) as probe:
            check_native_protections('npm-cache', ['npm'], [])
            self.assertEqual(probe.call_args.kwargs.get('cwd'), Path.home())

    def test_timeout_kills_descendant(self):
        from maintenance import live_command
        import subprocess
        import sys
        import time
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'should-not-exist'
            child = 'import time,pathlib; time.sleep(.6); pathlib.Path(' + repr(str(marker)) + ').touch()'
            parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(5)'
            with self.assertRaises(subprocess.TimeoutExpired):
                live_command([sys.executable, '-c', parent], timeout=.15)
            time.sleep(.7)
            self.assertFalse(marker.exists())

    def test_managed_missing_destination_fails_closed(self):
        from managed_cleanup import check_selected
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                check_selected('pyenv', [{'argv': ['pyenv', 'uninstall', '3.12'], 'path': None}], Policy(Path(tmp)))

    def test_managed_protected_version_blocks(self):
        from managed_cleanup import check_selected
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            version = home / '.pyenv/versions/3.12'
            version.mkdir(parents=True)
            policy = Policy(home, [version])
            with self.assertRaises(ValueError):
                check_selected('pyenv', [{'argv': ['pyenv', 'uninstall', '3.12'], 'path': str(version)}], policy)

    def test_pyenv_inventory_attaches_target_and_omits_active(self):
        from managed_cleanup import candidates
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patch('managed_cleanup.tool', return_value='/tools/pyenv'), patch('managed_cleanup.query', side_effect=[str(root), '3.13', '3.12\n3.13\nsystem\n']):
                rows = candidates('pyenv')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['path'], str(root / 'versions/3.12'))

    def test_plan_deletes_build_fixture_and_preserves_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            policy = Policy(home)
            target = home / 'Projects/demo/target'
            target.mkdir(parents=True)
            source = target.parent / 'Cargo.toml'
            source.write_text('[package]')
            (target / 'binary').write_text('generated')
            f = Finding('Builds', str(target), 'Cargo', '', action='delete-reviewed', identity=identity(target))
            plan = make_plan([f], policy)
            execute_plan(plan, policy, True)
            self.assertFalse(target.exists())
            self.assertTrue(source.exists())

    def test_ui_interruption_does_not_claim_no_cleanup(self):
        from fzf_ui import launch
        import io
        with tempfile.TemporaryDirectory() as tmp:
            policy = Policy(Path(tmp).resolve())
            report = {'findings': []}
            output = io.StringIO()
            with patch('fzf_ui.background', return_value=report), patch('fzf_ui.choose', return_value=[7]), patch('maintenance.menu', side_effect=KeyboardInterrupt), patch('fzf_ui.shutil.which', return_value='/tools/fzf'), patch('sys.stdout', output):
                launch(policy, policy.projects, 60, False, False, False)
            self.assertNotIn('Nenhuma limpeza executada', output.getvalue())
            self.assertIn('alterações parciais', output.getvalue())


if __name__ == '__main__':
    unittest.main()
