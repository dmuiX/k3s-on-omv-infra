"""Offline exact-version chart/Live-overlay regression checks; never print Secret data.

Run with the Ansible Python. Charts default to /tmp/platform-chart-audit;
PLATFORM_CHART_AUDIT and PLATFORM_LIVE_REPO may override local locations.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

INFRA = Path(__file__).resolve().parents[1]
LIVE = Path(os.environ.get('PLATFORM_LIVE_REPO', INFRA.parent / 'k3s-on-omv-live'))
CHARTS = Path(os.environ.get('PLATFORM_CHART_AUDIT', '/tmp/platform-chart-audit'))
MONITOR_API = 'monitoring.coreos.com/v1'


def load(path):
    return yaml.safe_load(path.read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def effective_values(app, source=None):
    source = source or app['spec']['sources'][0]
    def merge(base, override):
        result = copy.deepcopy(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result
    values = {}
    for value_file in source.get('helm', {}).get('valueFiles', []):
        if not value_file.startswith('$live/'):
            raise AssertionError('Expected a private Live value file')
        values = merge(values, load(LIVE / value_file.removeprefix('$live/')))
    return merge(values, source.get('helm', {}).get('valuesObject', {}))


def render(app, values=None, capabilities=True):
    source = app['spec']['sources'][0]
    chart = source['chart']
    version = source['targetRevision']
    chart_path = CHARTS / f'{chart}-{version}' / chart
    if not chart_path.is_dir():
        raise AssertionError(f'Exact chart not available: {chart_path}')
    command = ['helm', 'template', source.get('helm', {}).get('releaseName', app['metadata']['name']),
               str(chart_path), '--namespace', app['spec']['destination']['namespace'], '-f', '-']
    if capabilities:
        command += ['--api-versions', MONITOR_API, '--api-versions', 'gateway.networking.k8s.io/v1']
    result = subprocess.run(command, input=yaml.safe_dump(values if values is not None else effective_values(app)),
                            text=True, capture_output=True)
    # Do not include captured stdout/stderr in failures: renders can contain Secrets.
    if result.returncode:
        raise AssertionError(f'helm template failed for {chart} {version} (exit {result.returncode})')
    return [d for d in yaml.safe_load_all(result.stdout) if d and d['kind'] != 'Secret']


def kind(documents, name):
    return [d for d in documents if d['kind'] == name]


class NativeChartMigrations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Resolve the direct Infra Application inputs, with Helm's actual
        # valueFiles-before-valuesObject precedence; no legacy patch layer.
        cls.apps = {app['metadata']['name']: app for path in INFRA.glob('**/app.yml')
                    for app in [load(path)]}
        for app in cls.apps.values():
            source = app['spec'].get('sources', [app['spec'].get('source', {})])[0]
            if 'helm' in source:
                source['helm']['valuesObject'] = effective_values(app, source)

    def test_k8up_leader_and_monitor(self):
        app = self.apps['k8up']
        values = app['spec']['sources'][0]['helm']['valuesObject']
        self.assertNotIn('leaderElection', values)
        self.assertTrue(values['k8up']['enableLeaderElection'])
        docs = render(app)
        deployment, = kind(docs, 'Deployment')
        self.assertEqual(deployment['spec']['replicas'], 2)
        env = deployment['spec']['template']['spec']['containers'][0]['env']
        self.assertIn({'name': 'BACKUP_ENABLE_LEADER_ELECTION', 'value': 'true'}, env)
        self.check_service_monitors(docs, 1)

    def check_service_monitors(self, docs, expected):
        monitors = kind(docs, 'ServiceMonitor')
        self.assertEqual(len(monitors), expected)
        services = kind(docs, 'Service')
        for monitor in monitors:
            selector = monitor['spec']['selector']
            labels = selector.get('matchLabels', {})
            matches = [s for s in services if all(s['metadata'].get('labels', {}).get(k) == v for k, v in labels.items())]
            for expression in selector.get('matchExpressions', []):
                self.assertEqual(expression['operator'], 'In')
                matches = [s for s in matches if s['metadata'].get('labels', {}).get(expression['key']) in expression['values']]
            self.assertTrue(matches, 'Monitor must select a rendered Service')
            for endpoint in monitor['spec']['endpoints']:
                if 'port' in endpoint:
                    self.assertTrue(any(endpoint['port'] in [p['name'] for p in s['spec']['ports']] for s in matches))
                else:
                    workloads = kind(docs, 'Deployment') + kind(docs, 'StatefulSet')
                    pod_ports = [p['name'] for w in workloads for c in w['spec']['template']['spec']['containers']
                                 for p in c.get('ports', [])]
                    self.assertIn(endpoint['targetPort'], pod_ports)

    def test_cert_manager_and_cnpg(self):
        docs = render(self.apps['cert-manager'])
        self.check_service_monitors(docs, 1)
        self.assertFalse(kind(docs, 'PodMonitor'))
        docs = render(self.apps['cloudnative-pg'])
        monitor, = kind(docs, 'PodMonitor')
        self.assertEqual(monitor['metadata']['labels']['release'], 'kube-prometheus-stack')
        labels = monitor['spec']['selector']['matchLabels']
        deployment, = kind(docs, 'Deployment')
        pod_labels = deployment['spec']['template']['metadata']['labels']
        self.assertTrue(all(pod_labels.get(k) == v for k, v in labels.items()))
        ports = [p['name'] for c in deployment['spec']['template']['spec']['containers'] for p in c.get('ports', [])]
        for endpoint in monitor['spec']['podMetricsEndpoints']:
            self.assertIn(endpoint['port'], ports)

    def test_longhorn_only_adds_restricted_scrape_rule(self):
        app = self.apps['longhorn']
        values = copy.deepcopy(app['spec']['sources'][0]['helm']['valuesObject'])
        expected = {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-prometheus-stack'}},
                    'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'prometheus',
                                                   'prometheus': 'kube-prometheus-stack-prometheus'}}}
        self.assertEqual(values['networkPolicies']['metricsScrapeSources'], [expected])
        docs = render(app)
        self.check_service_monitors(docs, 1)
        values['networkPolicies'].pop('metricsScrapeSources')
        baseline = {d['metadata']['name']: d for d in kind(render(app, values), 'NetworkPolicy')}
        actual = {d['metadata']['name']: copy.deepcopy(d) for d in kind(docs, 'NetworkPolicy')}
        self.assertEqual(set(actual), set(baseline))
        removed = 0
        for policy in actual.values():
            ingress = policy['spec'].get('ingress', [])
            rule = {'from': [expected], 'ports': [{'protocol': 'TCP', 'port': 9500}]}
            if rule in ingress:
                ingress.remove(rule)
                removed += 1
        self.assertEqual(removed, 1)
        self.assertEqual(actual, baseline, 'All existing internal policies must remain unchanged')

    def test_argo_native_route_and_metrics(self):
        app = self.apps['argocd-config']
        docs = render(app)
        route, = kind(docs, 'HTTPRoute')
        self.assertEqual(route['metadata']['name'], 'argocd-server')
        # Snapshot of the retired route's complete spec, including explicit API defaults.
        self.assertEqual(digest(route['spec']), '5d1a10a2154dc373e291b841d6f77c63154c7531ac3d4bd1b6650bea4637ec80')
        self.assertFalse((LIVE / 'infra/argocd-config/route.yaml').exists())
        self.assertTrue((LIVE / 'infra/argocd-config/backend-tls.yaml').is_file())
        self.check_service_monitors(docs, 5)
        names = {d['metadata']['name'] for d in kind(docs, 'ServiceMonitor')}
        self.assertEqual(names, {'argocd-application-controller', 'argocd-server', 'argocd-repo-server',
                                 'argocd-applicationset-controller', 'argocd-notifications-controller'})
        self.assertFalse(kind(render(app, capabilities=False), 'ServiceMonitor'), 'Argo monitors are API-gated')
        values = app['spec']['sources'][0]['helm']['valuesObject']
        self.assertEqual(values['configs']['params']['server.insecure'], 'false')
        for component in ('dex', 'redis'):
            self.assertNotIn('metrics', values[component])
        redis = next(d for d in kind(docs, 'Deployment') if d['metadata']['name'] == 'argocd-redis')
        self.assertNotIn('redis-exporter', [c['name'] for c in redis['spec']['template']['spec']['containers']])

    def test_native_pdbs_preserve_replica_counts_and_protect_one_replica(self):
        longhorn = self.apps['longhorn']
        values = longhorn['spec']['sources'][0]['helm']['valuesObject']
        self.assertEqual(values['longhornUI']['replicas'], 2)
        pdbs = kind(render(longhorn), 'PodDisruptionBudget')
        self.assertEqual(len(pdbs), 1)
        self.assertEqual(pdbs[0]['spec']['minAvailable'], 1)
        self.assertNotIn('maxUnavailable', pdbs[0]['spec'])
        self.assertIn('ui', pdbs[0]['metadata']['name'])
        monitoring = self.apps['kube-prometheus-stack']
        values = monitoring['spec']['sources'][0]['helm']['valuesObject']
        for component, spec_key in [('prometheus', 'prometheusSpec'), ('alertmanager', 'alertmanagerSpec')]:
            self.assertEqual(values[component][spec_key]['replicas'],
                             0 if component == 'alertmanager' else 2)
            self.assertEqual(values[component]['podDisruptionBudget'], {
                'enabled': True, 'minAvailable': 1, 'unhealthyPodEvictionPolicy': 'IfHealthyBudget'})
        pdbs = kind(render(monitoring), 'PodDisruptionBudget')
        self.assertEqual(len(pdbs), 3, 'Two new native budgets plus the existing Grafana budget')
        for component in ('prometheus', 'alertmanager'):
            pdb, = [d for d in pdbs if d['metadata']['name'].endswith('-' + component)]
            self.assertEqual(pdb['spec']['minAvailable'], 1)
            self.assertEqual(pdb['spec']['unhealthyPodEvictionPolicy'], 'IfHealthyBudget')
            self.assertNotIn('maxUnavailable', pdb['spec'])
            self.assertTrue(pdb['spec']['selector']['matchLabels'])
        for name in ('crossplane', 'vault-secrets-operator', 'istio-core'):
            self.assertFalse(kind(render(self.apps[name]), 'PodDisruptionBudget'), name)

    def test_smb_exact_spec_and_single_owner(self):
        app = self.apps['csi-driver-smb']
        storage, = kind(render(app), 'StorageClass')
        self.assertEqual(storage['metadata']['name'], 'smb')
        spec = {k: v for k, v in storage.items() if k != 'metadata'}
        # Full retired spec includes every parameter and the ordered mountOptions.
        self.assertEqual(digest(spec), 'e72348ac930271ac0fec8456a6f75ff4017c3978bf9ec132d6d2ee41894fd551')
        self.assertEqual(set(storage['metadata']['labels']), {'app.kubernetes.io/instance', 'app.kubernetes.io/managed-by',
                                                            'app.kubernetes.io/name', 'app.kubernetes.io/version', 'helm.sh/chart'})
        self.assertFalse((LIVE / 'infra/csi-driver-smb/storageclass.yaml').exists())
        self.assertFalse(any(s.get('path') == 'infra/csi-driver-smb' for s in app['spec']['sources']))
        self.assertTrue(any(s.get('path') == '02-controllers/csi-driver-smb/resources' for s in app['spec']['sources']))


if __name__ == '__main__':
    unittest.main()
