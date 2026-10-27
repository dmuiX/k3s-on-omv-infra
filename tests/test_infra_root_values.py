"""Preserve legacy private overrides when the Infra root owns Applications directly."""
from copy import deepcopy
from pathlib import Path
import fnmatch
import json
import os
import shutil
import subprocess
import unittest

import yaml

INFRA = Path(__file__).resolve().parents[1]
LIVE = INFRA.parent / 'k3s-on-omv-live'
APPLICATIONS = {
    'argocd-config': '01-bootstrap/argocd-config/app.yml',
    'longhorn': '02-controllers/longhorn/app.yml',
    'openbao': '04-openbao/app.yml',
    'kube-prometheus-stack': '09-kube-prometheus-stack/app.yml',
}


def merge(base, overrides):
    result = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def values(name):
    app = yaml.safe_load((INFRA / APPLICATIONS[name]).read_text())
    source = app['spec']['sources'][0]
    before = deepcopy(source['helm']['valuesObject'])
    patches = yaml.safe_load((LIVE / f'infra/applications/patches/{name}.yaml').read_text())
    for operation in patches:
        assert operation['op'] == 'add'
        prefix = '/spec/sources/0/helm/valuesObject/'
        assert operation['path'].startswith(prefix)
        keys = operation['path'][len(prefix):].split('/')
        node = before
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = operation['value']
    private = yaml.safe_load((LIVE / f'infra/{name}/values.yml').read_text())
    # Argo CD gives valuesObject higher precedence than valueFiles.
    after = merge(private, source['helm']['valuesObject'])
    return app, source, before, after


class InfraRootValuesTest(unittest.TestCase):
    def test_legacy_settings_survive_additional_native_chart_configuration(self):
        def preserved(expected, actual):
            if isinstance(expected, dict):
                self.assertIsInstance(actual, dict)
                for key, value in expected.items():
                    self.assertIn(key, actual)
                    preserved(value, actual[key])
            else:
                self.assertEqual(expected, actual)
        for name in APPLICATIONS:
            with self.subTest(application=name):
                _, _, before, after = values(name)
                preserved(before, after)

    def test_private_sources_and_paths_are_resolvable(self):
        for name in APPLICATIONS:
            with self.subTest(application=name):
                app, source, _, _ = values(name)
                refs = [s for s in app['spec']['sources'] if s.get('ref') == 'live']
                self.assertEqual(len(refs), 1)
                self.assertEqual(refs[0]['repoURL'], 'https://github.com/dmuiX/k3s-on-omv-live.git')
                if name == 'openbao':
                    route, = [s for s in app['spec']['sources'] if s.get('path') == 'charts/service-route']
                    self.assertEqual(route['helm']['valueFiles'], ['$live/infra/openbao/route-values.yml'])
                    self.assertFalse(source['helm']['valuesObject']['server']['gateway']['httpRoute']['enabled'])
                else:
                    self.assertEqual(source['helm']['valueFiles'], [f'$live/infra/{name}/values.yml'])
                private_file = LIVE / f'infra/{name}/values.yml'
                self.assertTrue(private_file.is_file())
                # Supported by the installed Argo CD 3.5.5 directory renderer:
                # the same file remains usable as Helm input, but is never a
                # raw Kubernetes manifest, including before root migration.
                self.assertEqual(private_file.read_text().splitlines()[0],
                                 '# +argocd:skip-file-rendering')

    def test_root_selects_all_children_but_not_itself_or_resources(self):
        source = yaml.safe_load((INFRA / 'infra.yml').read_text())['spec']['source']
        self.assertEqual(source['repoURL'], 'https://github.com/dmuiX/k3s-on-omv-infra.git')
        self.assertEqual(source['path'], '.')
        self.assertTrue(source['directory']['recurse'])
        patterns = source['directory']['include'].strip('{}').split(',')
        selected = {str(p.relative_to(INFRA)) for p in INFRA.rglob('*.yml')
                    if any(fnmatch.fnmatch(str(p.relative_to(INFRA)), pattern) for pattern in patterns)}
        self.assertEqual(selected, {str(p.relative_to(INFRA)) for p in INFRA.rglob('app.yml')})
        self.assertNotIn('infra.yml', selected)

    @unittest.skipUnless(os.environ.get('HELM_CHART_CACHE') and shutil.which('helm'),
                         'Set HELM_CHART_CACHE to exact-version chart archives for render validation')
    def test_exact_charts_render_with_private_values(self):
        for name in APPLICATIONS:
            with self.subTest(application=name):
                app, source, _, after = values(name)
                archive = Path(os.environ['HELM_CHART_CACHE']) / f"{source['chart']}-{source['targetRevision']}.tgz"
                self.assertTrue(archive.is_file())
                command = ['helm', 'template', source['helm'].get('releaseName', name), str(archive),
                           '--namespace', app['spec']['destination']['namespace'],
                           '--api-versions', 'gateway.networking.k8s.io/v1/HTTPRoute',
                           '--values', '-']
                result = subprocess.run(command, input=json.dumps(after), capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, f'{name} chart rendering failed')
                resources = [d for d in yaml.safe_load_all(result.stdout) if d]
                self.assertTrue(resources)
                for resource in resources:
                    if resource.get('kind') == 'HTTPRoute':
                        for parent in resource['spec'].get('parentRefs', []):
                            self.assertEqual(parent['group'], 'gateway.networking.k8s.io')
                            self.assertEqual(parent['kind'], 'Gateway')
                        for rule in resource['spec'].get('rules', []):
                            for backend in rule.get('backendRefs', []):
                                # OpenBao 0.30.4 hardcodes its backend and omits
                                # group/weight; this migration preserves that
                                # existing chart behavior rather than replacing it.
                                self.assertEqual(backend.get('group', ''), '')
                                self.assertEqual(backend['kind'], 'Service')
                                self.assertEqual(backend.get('weight', 1), 1)


if __name__ == '__main__':
    unittest.main()
