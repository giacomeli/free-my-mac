import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


class AuditTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('dev_audit'), 'módulo de auditoria necessário')
        import dev_audit
        return dev_audit

    def test_protection_includes_parent_and_descendant(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            p = a.Policy(Path(d), [Path(d) / 'work/artifacts'])
            self.assertTrue(p.protected(Path(d) / 'work'))
            self.assertTrue(p.protected(Path(d) / 'work/artifacts/model'))
            self.assertFalse(p.protected(Path(d) / 'cache'))

    def test_old_project_and_recent_source(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project = root / 'projects/app'
            project.mkdir(parents=True)
            (project / 'node_modules').mkdir()
            (project / 'package.json').write_text('{}')
            (project / 'package-lock.json').write_text('{}')
            old = time.time() - 100 * 86400
            for p in project.iterdir():
                os.utime(p, (old, old))
            findings = a.project_findings(root / 'projects', a.Policy(root), 60)
            self.assertEqual(len(findings), 1)
            self.assertIn('inatividade', findings[0].reason)
            (project / 'main.js').write_text('recent')
            findings = a.project_findings(root / 'projects', a.Policy(root), 60)
            self.assertIn('recente', findings[0].reason)

    def test_symlink_cache_not_executable(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'outside').mkdir()
            (root / '.npm').symlink_to(root / 'outside')
            items = a.catalog_findings(a.Policy(root))
            self.assertFalse(any(x.action == 'delete-cache' for x in items))

    def test_plan_changed_target_rejected_and_dry_run_preserves(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / '.npm/_cacache'
            cache.mkdir(parents=True)
            (cache / 'data').write_text('keep')
            policy = a.Policy(root)
            finding = next(x for x in a.catalog_findings(policy) if x.action == 'delete-cache')
            plan = a.make_plan([finding], policy)
            self.assertIn('simulação', a.execute_plan(plan, policy, False)[0])
            self.assertTrue(cache.exists())
            plan['items'][0]['path'] = str(root)
            with self.assertRaises(ValueError):
                a.execute_plan(plan, policy, True)

    def test_explicit_cache_execution_only_in_temporary_fixture(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / '.npm/_cacache'
            cache.mkdir(parents=True)
            (cache / 'data').write_text('fixture')
            policy = a.Policy(root)
            f = next(x for x in a.catalog_findings(policy) if x.action == 'delete-cache')
            a.execute_plan(a.make_plan([f], policy), policy, True)
            self.assertFalse(cache.exists())

    def test_history_unknown_sizes_not_zero(self):
        a = self.api()
        self.assertEqual(a.compare_reports({'findings':[{'path':'a','size':None}]},
                                           {'findings':[{'path':'a','size':42}]}), [])

    def test_nested_sketchpad_blocks_cache_removal(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / '.npm/_cacache'
            cache.mkdir(parents=True)
            (cache / 'sketchpad.bin').write_text('important')
            policy = a.Policy(root)
            f = next(x for x in a.catalog_findings(policy) if x.action == 'delete-cache')
            with self.assertRaises(ValueError):
                a.execute_plan(a.make_plan([f], policy), policy, True)
            self.assertTrue((cache / 'sketchpad.bin').exists())

    def test_project_inspection_does_not_traverse_protected_child(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project = root / 'projects/protected/app'
            project.mkdir(parents=True)
            (project / 'package.json').write_text('{}')
            (project / 'node_modules').mkdir()
            self.assertEqual(a.project_findings(root / 'projects', a.Policy(root, [root / 'projects/protected']), 60), [])

    def test_dependency_references_are_collected_without_running_project_code(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project = root / 'projects/app'
            project.mkdir(parents=True)
            (project / '.nvmrc').write_text('20')
            (project / 'global.json').write_text('{"sdk":{"version":"8.0.100"}}')
            self.assertTrue(hasattr(a, 'project_requirements'))
            requirements = a.project_requirements(root / 'projects', a.Policy(root))
            self.assertEqual(requirements[str(project / '.nvmrc')], '20')
            self.assertIn('8.0.100', requirements[str(project / 'global.json')])

    def test_protected_project_does_not_hide_siblings(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ('keep', 'audit'):
                p = root / 'projects' / name
                p.mkdir(parents=True)
                (p / 'package.json').write_text('{}')
                (p / 'node_modules').mkdir()
            items = a.project_findings(root / 'projects', a.Policy(root, [root / 'projects/keep']), 60)
            self.assertEqual(len(items), 1)
            self.assertIn('/audit/', items[0].path)

    def test_plan_preserves_configuration_and_detects_changed_child(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / '.npm/_cacache'
            cache.mkdir(parents=True)
            child = cache / 'data'
            child.write_text('original')
            policy = a.Policy(root, [root / 'keep'])
            f = next(x for x in a.catalog_findings(policy) if x.action == 'delete-cache')
            plan = a.make_plan([f], policy)
            with self.assertRaises(ValueError):
                a.execute_plan(plan, a.Policy(root), True)
            child.write_text('modified after plan')
            with self.assertRaises(ValueError):
                a.execute_plan(plan, policy, True)

    def test_old_node_modules_plan_requires_lock_and_rechecks_activity(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = root / 'Projects/app'
            p.mkdir(parents=True)
            (p / 'package.json').write_text('{}')
            (p / 'package-lock.json').write_text('{}')
            (p / 'node_modules').mkdir()
            old = time.time() - 100 * 86400
            for f in p.iterdir():
                os.utime(f, (old, old))
            policy = a.Policy(root)
            f = a.project_findings(root / 'Projects', policy, 60)[0]
            self.assertEqual(f.action, 'delete-node-modules')
            plan = a.make_plan([f], policy)
            (p / 'new-source.js').write_text('active')
            with self.assertRaises(ValueError):
                a.execute_plan(plan, policy, True)

    def test_cancelled_audit_does_not_start_probes(self):
        import threading
        a = self.api()
        stop = threading.Event()
        stop.set()
        with tempfile.TemporaryDirectory() as d, patch.object(a, 'run', side_effect=AssertionError('probe após cancelamento')):
            report = a.audit(a.Policy(Path(d)), Path(d), stop=stop)
            self.assertEqual(report['tools'], {})

    def test_docker_override_is_not_contacted(self):
        a = self.api()
        with patch.dict(os.environ, {'DOCKER_HOST': 'ssh://remote'}), patch.object(a.shutil, 'which', return_value='/usr/bin/docker'), patch.object(a, 'run', side_effect=AssertionError('remote contacted')):
            self.assertFalse(a.docker_inventory()['ok'])

    def test_output_read_only_report_schema(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d, patch('audit_adapters.app_inventory', return_value={}):
            root = Path(d)
            (root / '.npm/_cacache').mkdir(parents=True)
            result = a.audit(a.Policy(root), root / 'Projects', probes=False)
            self.assertEqual(result['schema'], 1)
            self.assertTrue((root / '.npm/_cacache').is_dir())
            self.assertTrue(result['findings'])

    def test_journal_records_partial_failure(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / '.npm/_cacache').mkdir(parents=True)
            policy = a.Policy(root)
            plan = a.make_plan([a.catalog_findings(policy)[0]], policy)
            events = []
            with patch.object(a.shutil, 'rmtree', side_effect=OSError('fixture failure')) as delete:
                delete.avoids_symlink_attacks = True
                results = a.execute_plan(plan, policy, True, events.append)
            self.assertIn('falha parcial', results[0])
            self.assertEqual(events[-1]['status'], 'falha parcial possível')

    def test_node_modules_execution_preserves_source_and_external_link_target(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = root / 'Projects/app'
            p.mkdir(parents=True)
            (p / 'package.json').write_text('{}')
            (p / 'package-lock.json').write_text('{}')
            (p / 'main.js').write_text('source')
            deps = p / 'node_modules'
            deps.mkdir()
            external = root / 'external'
            external.write_text('keep')
            (deps / 'linked').symlink_to(external)
            old = time.time() - 100 * 86400
            for f in p.iterdir():
                os.utime(f, (old, old))
            policy = a.Policy(root)
            f = a.project_findings(root / 'Projects', policy, 60)[0]
            result = a.execute_plan(a.make_plan([f], policy), policy, True)
            self.assertTrue(result[0].startswith('removido'))
            self.assertFalse(deps.exists())
            self.assertEqual((p / 'main.js').read_text(), 'source')
            self.assertEqual(external.read_text(), 'keep')

    def test_uninspected_protected_sources_do_not_imply_inactivity(self):
        a = self.api()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = root / 'Projects/app'
            (p / 'src/private').mkdir(parents=True)
            (p / 'src/private/active.js').write_text('active')
            (p / 'node_modules').mkdir()
            for name in ('package.json', 'package-lock.json'):
                f = p / name
                f.write_text('{}')
                old = time.time() - 100 * 86400
                os.utime(f, (old, old))
            rows = a.project_findings(root / 'Projects', a.Policy(root, [p / 'src/private']), 60)
            self.assertEqual(rows[0].action, 'review')


if __name__ == '__main__':
    unittest.main()
