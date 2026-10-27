"""Offline service-route contracts; fixture captured before migration.

Uses the exact OpenBao chart and actual Live patches through offline Kustomize.
Only hostname digests are stored in this public repository. No Secret is saved
or printed. Run with the Ansible Python and the same cache as native migrations.
"""
import copy
import subprocess
import unittest

import yaml

import test_native_chart_migrations as native
from test_native_chart_migrations import INFRA, LIVE, digest, kind, load, render

CHART = INFRA / 'charts/service-route'
BASELINE = load(INFRA / 'tests/fixtures/service-route/baseline.json')


def route_render(values):
    result = subprocess.run(
        ['helm', 'template', 'irrelevant-release', str(CHART), '-f', '-'],
        input=yaml.safe_dump(values), text=True, capture_output=True,
    )
    return result


class ServiceRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        native.NativeChartMigrations.setUpClass()
        cls.apps = native.NativeChartMigrations.apps

    def values(self, app_name):
        app = self.apps[app_name]
        source, = [s for s in app['spec']['sources'] if s.get('path') == 'charts/service-route']
        self.assertEqual(len([s for s in app['spec']['sources'] if s.get('ref') == 'live']), 1)
        return native.effective_values(app, source)

    def documents(self, values):
        result = route_render(values)
        self.assertEqual(result.returncode, 0, 'Service route chart must render')
        docs = list(yaml.safe_load_all(result.stdout))
        self.assertEqual([d['kind'] for d in docs], ['HTTPRoute'], 'No generated Secrets or other resources')
        return docs

    def check_baseline(self, name, app_name):
        route, = self.documents(self.values(app_name))
        baseline = copy.deepcopy(BASELINE[name])
        self.assertEqual({k: route[k] for k in ('apiVersion', 'kind')}, baseline['identity'])
        self.assertEqual(route['metadata']['name'], baseline['name'])
        self.assertEqual(route['metadata']['namespace'], baseline['namespace'])
        spec = copy.deepcopy(route['spec'])
        self.assertEqual(digest(spec.pop('hostnames')), baseline['hostnameDigest'])
        if name == 'openbao':
            # The sole spec change: pin previously implicit Service API defaults.
            backend = baseline['spec']['rules'][0]['backendRefs'][0]
            backend.update(group='', weight=1)
        self.assertEqual(spec, baseline['spec'])
        for parent in spec['parentRefs']:
            self.assertEqual((parent['group'], parent['kind']), ('gateway.networking.k8s.io', 'Gateway'))
        for rule in spec['rules']:
            for backend in rule['backendRefs']:
                self.assertEqual((backend['group'], backend['kind'], backend['weight']), ('', 'Service', 1))

    def test_openbao_exact_identity_and_spec(self):
        self.check_baseline('openbao', 'openbao')

    def test_longhorn_exact_identity_and_spec(self):
        self.check_baseline('longhorn', 'longhorn-config')

    def test_longhorn_configmap_and_single_route_owner(self):
        app = self.apps['longhorn-config']
        source, = [s for s in app['spec']['sources'] if s.get('path') == 'charts/oidc-proxy']
        result = subprocess.run(['helm', 'template', 'proxy', str(INFRA / source['path']), '-f', '-'],
                                input=yaml.safe_dump(native.effective_values(app, source)),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        docs = [d for d in yaml.safe_load_all(result.stdout) if d]
        self.assertFalse(kind(docs, 'HTTPRoute'))
        configmap, = kind(docs, 'ConfigMap')
        self.assertEqual(digest(configmap), BASELINE['longhornConfigMapDigest'])

    def test_native_openbao_route_disabled(self):
        app = self.apps['openbao']
        self.assertIs(app['spec']['sources'][0]['helm']['valuesObject']['server']['gateway']['httpRoute']['enabled'], False)
        self.assertFalse(kind(render(app), 'HTTPRoute'), 'Exactly one route owner')

    def test_optional_typed_fields_preserved(self):
        values = self.values('longhorn-config')
        rule = values['rules'][0]
        rule['timeouts'] = {'request': '30s', 'backendRequest': '10s'}
        rule['filters'] = [{'type': 'RequestHeaderModifier', 'requestHeaderModifier': {
            'set': [{'name': 'X-Forwarded-Proto', 'value': 'https'}]}}]
        route, = self.documents(values)
        for field in ('matches', 'filters', 'timeouts'):
            self.assertEqual(route['spec']['rules'][0][field], rule[field])

    def test_invalid_fields_rejected(self):
        base = self.values('longhorn-config')
        mutations = [
            lambda v: v.update(manifests=[]),
            lambda v: v.update(name=''),
            lambda v: v.update(namespace=12),
            lambda v: v.update(hostnames=['https://invalid']),
            lambda v: v.update(parentRefs=[]),
            lambda v: v['parentRefs'][0].update(kind='Service'),
            lambda v: v['rules'][0]['backend'].update(port=0),
            lambda v: v['rules'][0]['backend'].update(port='80'),
            lambda v: v['rules'][0]['backend'].update(weight=-1),
            lambda v: v['rules'][0]['backend'].update(group='other'),
            lambda v: v['rules'][0].update(backendRefs=[]),
            lambda v: v['rules'][0].update(timeouts={'unknown': '1s'}),
            lambda v: v['rules'][0].update(timeouts={'request': 'forever'}),
            lambda v: v['rules'][0].update(filters=[{'type': 'ExtensionRef'}]),
            lambda v: v['rules'][0].update(filters=[{'type': 'RequestHeaderModifier'}]),
            lambda v: v['rules'][0].update(matches=[{'path': {'type': 'Bad', 'value': '/'}}]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                values = copy.deepcopy(base)
                mutate(values)
                self.assertNotEqual(route_render(values).returncode, 0)

    def test_public_chart_and_values_are_generic(self):
        source = next(s for s in self.apps['openbao']['spec']['sources'] if s.get('path') == 'charts/service-route')
        # Actual private hostname is supplied by Live, not public chart defaults.
        public = load(CHART / 'values.yaml')
        private = native.effective_values(self.apps['openbao'], source)
        self.assertNotEqual(public['hostnames'], private['hostnames'])
        for hostname in private['hostnames']:
            self.assertNotIn(hostname, (INFRA / '04-openbao/app.yml').read_text())
        for path in CHART.rglob('*'):
            if path.is_file():
                self.assertNotIn('h-lab.org', path.read_text())


if __name__ == '__main__':
    unittest.main()
