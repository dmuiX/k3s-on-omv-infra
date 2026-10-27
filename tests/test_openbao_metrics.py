"""Offline contracts for native OpenBao metrics and Ambient identity isolation.

Run with Ansible's Python; pinned chart caches and isolated Live checkout required.
No cluster access and no credential values are read or printed.
"""
import os
from pathlib import Path
import re
import unittest

import yaml

import test_native_chart_migrations as native

ROOT = Path(__file__).resolve().parents[1]
PRINCIPAL = 'cluster.local/ns/kube-prometheus-stack/sa/kube-prometheus-stack-prometheus'
METRICS_PEER = {
    'namespaceSelector': {'matchLabels': {
        'kubernetes.io/metadata.name': 'kube-prometheus-stack'}},
    'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'prometheus'}},
}
METRICS_INGRESS = {'from': [METRICS_PEER],
                   'ports': [{'protocol': 'TCP', 'port': 9101}]}


class OpenBaoMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = native.load(ROOT / '04-openbao/app.yml')
        cls.values = native.effective_values(cls.app)
        cls.docs = native.render(cls.app)
        cls.policy, = yaml.safe_load_all(
            (ROOT / '04-openbao/resources/metrics.yaml').read_text())

    def test_separate_native_listeners_and_ha_preserved(self):
        server = self.values['server']
        config = server['ha']['raft']['config']
        listeners = re.findall(r'listener "tcp" \{(.*?)\n\}', config, re.S)
        self.assertEqual(len(listeners), 2)
        api, metrics = listeners
        self.assertIn('address = "[::]:8200"', api)
        self.assertIn('disallow_metrics = true', api)
        self.assertNotIn('unauthenticated_metrics_access', api)
        self.assertIn('address = "[::]:9101"', metrics)
        self.assertIn('metrics_only = true', metrics)
        self.assertIn('unauthenticated_metrics_access = true', metrics)
        self.assertNotIn('metrics_path', config)
        self.assertIn('prometheus_retention_time = "30s"', config)
        self.assertIn('disable_hostname = true', config)
        self.assertTrue(server['ha']['raft']['enabled'])
        self.assertEqual(server['ha']['replicas'], 3)
        pdb, = native.kind(self.docs, 'PodDisruptionBudget')
        self.assertEqual(pdb['spec']['maxUnavailable'], 1)
        sts, = native.kind(self.docs, 'StatefulSet')
        self.assertEqual(sts['spec']['updateStrategy']['type'], 'OnDelete')
        self.assertIn(':2.7.1', sts['spec']['template']['spec']['containers'][0]['image'])

    def test_pinned_monitor_and_active_service_have_named_metrics_port(self):
        self.assertEqual(self.app['spec']['sources'][0]['targetRevision'], '0.30.4')
        self.assertEqual(self.values['serverTelemetry']['serviceMonitor'], {
            'enabled': True, 'port': 'metrics'})
        monitor, = native.kind(self.docs, 'ServiceMonitor')
        self.assertEqual(monitor['spec']['namespaceSelector']['matchNames'], ['openbao'])
        self.assertEqual(monitor['spec']['selector']['matchLabels']['openbao-active'], 'true')
        endpoint, = monitor['spec']['endpoints']
        self.assertEqual(endpoint['port'], 'metrics')
        self.assertEqual(endpoint['path'], '/v1/sys/metrics')
        self.assertFalse({'authorization', 'bearerTokenFile', 'bearerTokenSecret',
                          'basicAuth', 'oauth2'} & endpoint.keys())
        active, = [s for s in native.kind(self.docs, 'Service')
                   if s['metadata']['name'] == 'openbao-active']
        self.assertEqual(active['spec']['selector']['openbao-active'], 'true')
        self.assertIn({'name': 'metrics', 'port': 9101, 'targetPort': 'metrics',
                       'protocol': 'TCP'}, active['spec']['ports'])
        for service in native.kind(self.docs, 'Service'):
            if any(p['port'] == 9101 for p in service['spec']['ports']):
                self.assertEqual(service['spec'].get('type', 'ClusterIP'), 'ClusterIP')
                self.assertFalse(service['spec'].get('externalIPs'))
        sts, = native.kind(self.docs, 'StatefulSet')
        self.assertIn({'name': 'metrics', 'containerPort': 9101, 'protocol': 'TCP'},
                      sts['spec']['template']['spec']['containers'][0]['ports'])
        self.assertFalse(native.kind(self.docs, 'HTTPRoute'))
        route = self.app['spec']['sources'][2]['helm']['valuesObject']
        self.assertEqual([r['backend']['port'] for r in route['rules']], [8200])

    def test_l4_deny_is_port_scoped_and_persistent_before_workload_sync(self):
        self.assertEqual(self.policy['apiVersion'], 'security.istio.io/v1')
        self.assertEqual(self.policy['kind'], 'AuthorizationPolicy')
        self.assertEqual(self.policy['metadata']['namespace'], 'openbao')
        annotations = self.policy['metadata']['annotations']
        self.assertLess(int(annotations['argocd.argoproj.io/sync-wave']), 0)
        self.assertNotIn('argocd.argoproj.io/hook', annotations)
        self.assertEqual(self.policy['spec'], {
            'selector': {'matchLabels': {'app.kubernetes.io/name': 'openbao',
                                        'app.kubernetes.io/instance': 'openbao',
                                        'component': 'server'}},
            'action': 'DENY',
            'rules': [{'from': [{'source': {'notPrincipals': [PRINCIPAL]}}],
                       'to': [{'operation': {'ports': ['9101']}}]}],
        })
        sts, = native.kind(self.docs, 'StatefulSet')
        labels = sts['spec']['template']['metadata']['labels']
        for key, value in self.policy['spec']['selector']['matchLabels'].items():
            self.assertEqual(labels[key], value)
        # Exact negative matching includes absent identity, unlike requiring '*'
        # as an additional positive principal match. Model the upstream L4 rule.
        source = self.policy['spec']['rules'][0]['from'][0]['source']
        for principal in ('', 'cluster.local/ns/kube-prometheus-stack/sa/grafana',
                          'cluster.local/ns/openbao/sa/openbao', PRINCIPAL):
            for port in (8200, 8201, 9101):
                denied = port == 9101 and principal not in source['notPrincipals']
                self.assertEqual(denied, port == 9101 and principal != PRINCIPAL)

    def test_direct_network_peer_and_shared_hbone_are_distinct(self):
        policy, = native.kind(self.docs, 'NetworkPolicy')
        ingress = policy['spec']['ingress']
        self.assertEqual([r for r in ingress if any(
            p['port'] == 9101 for p in r.get('ports', []))], [METRICS_INGRESS])
        self.assertTrue(any(any(p['port'] == 15008 for p in r.get('ports', []))
                            for r in ingress))
        # Existing API/Raft ingress must still exist; full baseline lives next door.
        self.assertTrue(any(any(p['port'] == 8200 for p in r.get('ports', []))
                            for r in ingress))
        self.assertTrue(any(any(p['port'] == 8201 for p in r.get('ports', []))
                            for r in ingress))

    def test_prometheus_identity_from_exact_monitoring_chart_with_live_inputs(self):
        app = native.load(ROOT / '09-kube-prometheus-stack/app.yml')
        self.assertEqual(app['spec']['sources'][0]['targetRevision'], '92.10.1')
        docs = native.render(app)
        prometheus, = native.kind(docs, 'Prometheus')
        sa = prometheus['spec']['serviceAccountName']
        self.assertEqual(PRINCIPAL, 'cluster.local/ns/' +
                         app['spec']['destination']['namespace'] + '/sa/' + sa)
        self.assertIn(sa, [s['metadata']['name'] for s in native.kind(docs, 'ServiceAccount')])
        self.assertEqual(app['spec']['syncPolicy']['managedNamespaceMetadata']['labels']
                         ['istio.io/dataplane-mode'], 'ambient')
        self.assertEqual(self.app['spec']['syncPolicy']['managedNamespaceMetadata']['labels']
                         ['istio.io/dataplane-mode'], 'ambient')

    def test_no_metrics_token_producers_or_provider_grants(self):
        self.assertFalse(self.app['spec']['syncPolicy']['automated']['prune'])
        self.assertEqual(self.policy['kind'], 'AuthorizationPolicy')
        docs = [d for p in (ROOT / '04-openbao/resources').glob('*.yaml')
                for d in yaml.safe_load_all(p.read_text()) if d]
        self.assertFalse(any(d['kind'] != 'AuthorizationPolicy' and
                             d['metadata']['name'] == 'openbao-metrics' for d in docs))

    def test_bootstrap_metrics_grants_retired_when_checkout_available(self):
        bootstrap = Path(os.environ.get('PLATFORM_BOOTSTRAP_REPO',
                                       ROOT.parent / 'k3s-on-omv-bootstrap'))
        policy = bootstrap / 'ansible/roles/openbao_setup/files/crossplane-provider-vault.hcl'
        if not policy.exists():
            self.skipTest('requires sibling Bootstrap checkout or PLATFORM_BOOTSTRAP_REPO')
        text = policy.read_text()
        self.assertNotIn('openbao-metrics', text)
        for path in ('sys/policies/acl/grafana-credentials',
                     'auth/kubernetes/role/grafana-credentials'):
            self.assertIn('path "' + path + '"', text)

    def test_rollout_documents_security_and_orphan_retirement_gates(self):
        text = (ROOT / 'docs/openbao-metrics-rollout.md').read_text()
        for phrase in ('policy-only', 'OnDelete', 'Orphan', 'VaultDynamicSecret',
                       'VaultAuth', 'VaultConnection', '15008', 'plaintext',
                       '24-hour', 'sync-wave', 'hook', 'not validated against a running cluster'):
            self.assertIn(phrase, text)


if __name__ == '__main__':
    unittest.main()
