#!/opt/homebrew/Cellar/ansible/14.5.0/libexec/bin/python
"""Offline contracts for the pinned native VSO self-lookup metrics lifecycle."""
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHART = pathlib.Path('/tmp/platform-chart-audit/openbao-0.30.4/openbao')
VSO_CHART = pathlib.Path('/tmp/platform-chart-audit/vault-secrets-operator-1.6.0/vault-secrets-operator')


class OpenBaoMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = yaml.safe_load((ROOT / '04-openbao/app.yml').read_text())
        cls.values = cls.app['spec']['sources'][0]['helm']['valuesObject']
        cls.docs = list(yaml.safe_load_all(
            (ROOT / '04-openbao/resources/metrics.yaml').read_text()))
        cls.by_kind = {d['kind']: d for d in cls.docs}

    def test_authenticated_telemetry_and_existing_ha(self):
        server = self.values['server']
        self.assertEqual(server['ha']['replicas'], 3)
        self.assertTrue(server['ha']['raft']['enabled'])
        config = server['ha']['raft']['config']
        self.assertIn('prometheus_retention_time = "30s"', config)
        self.assertIn('disable_hostname = true', config)
        self.assertNotIn('unauthenticated_metrics_access', config)
        self.assertEqual(server['ha']['disruptionBudget'], {
            'enabled': True, 'maxUnavailable': 1})

    def test_policy_is_exactly_metrics_and_own_lookup_renew(self):
        policy = self.by_kind['Policy']['spec']
        self.assertEqual(policy['providerConfigRef'], {'name': 'openbao'})
        self.assertEqual(policy['forProvider']['name'], 'openbao-metrics')
        acl = policy['forProvider']['policy']
        blocks = re.findall(r'path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^]]+)\]\s*\}', acl)
        self.assertEqual(len(blocks), 3)
        self.assertEqual({path: re.findall(r'"([^"]+)"', caps)
                          for path, caps in blocks}, {
            'sys/metrics': ['read'],
            'auth/token/lookup-self': ['read'],
            'auth/token/renew-self': ['update'],
        })
        # Reject any extra HCL statement, not just extra path blocks.
        self.assertFalse(re.sub(r'path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^]]+)\]\s*\}', '', acl).strip())

    def test_role_and_login_are_dedicated_and_bounded(self):
        self.assertEqual(self.by_kind['AuthBackendRole']['spec']['forProvider'], {
            'backend': 'kubernetes', 'roleName': 'openbao-metrics',
            'boundServiceAccountNames': ['openbao-metrics'],
            'boundServiceAccountNamespaces': ['openbao'],
            'tokenPolicies': ['openbao-metrics'], 'tokenNoDefaultPolicy': True,
            'tokenTtl': 3600, 'tokenMaxTtl': 86400,
        })
        self.assertEqual(self.by_kind['AuthBackendRole']['spec']['providerConfigRef'],
                         {'name': 'openbao'})
        self.assertFalse(self.by_kind['ServiceAccount']['automountServiceAccountToken'])
        self.assertEqual(self.by_kind['VaultAuth']['spec'], {
            'method': 'kubernetes', 'mount': 'kubernetes',
            'kubernetes': {'role': 'openbao-metrics',
                           'serviceAccount': 'openbao-metrics',
                           'tokenExpirationSeconds': 600},
            'vaultConnectionRef': 'openbao-metrics',
        })
        self.assertEqual(self.by_kind['VaultConnection']['spec'], {
            'address': 'http://openbao-active.openbao.svc.cluster.local:8200'})
        for kind in ('ServiceAccount', 'VaultAuth', 'VaultConnection', 'VaultDynamicSecret'):
            self.assertEqual(self.by_kind[kind]['metadata'], {
                'name': 'openbao-metrics', 'namespace': 'openbao'})

    def test_native_self_lookup_transforms_only_own_token(self):
        spec = self.by_kind['VaultDynamicSecret']['spec']
        self.assertEqual(set(spec), {'vaultAuthRef', 'mount', 'path',
                                    'requestHTTPMethod', 'refreshAfter', 'destination'})
        self.assertEqual(spec['vaultAuthRef'], 'openbao-metrics')
        self.assertEqual((spec['mount'], spec['path'], spec['requestHTTPMethod']),
                         ('auth', 'token/lookup-self', 'GET'))
        self.assertEqual(spec['refreshAfter'], '1m')
        destination = spec['destination']
        self.assertEqual({k: v for k, v in destination.items() if k != 'transformation'},
                         {'create': True, 'name': 'openbao-metrics', 'type': 'Opaque'})
        transform = destination['transformation']
        self.assertEqual(set(transform), {'excludeRaw', 'templates'})
        self.assertTrue(transform['excludeRaw'])
        self.assertEqual(set(transform['templates']), {'token'})
        self.assertEqual(transform['templates']['token']['text'],
                         '{{- $value := get .Secrets "id" -}}\n'
                         '{{- if empty $value -}}\n'
                         '{{- fail "missing required OpenBao self-lookup id" -}}\n'
                         '{{- end -}}\n{{- $value -}}')

    def test_no_provider_issuance_or_parallel_secret_writer(self):
        self.assertEqual(len(self.docs), 6)
        self.assertEqual(set(self.by_kind), {'Policy', 'AuthBackendRole', 'ServiceAccount',
                                           'VaultConnection', 'VaultAuth', 'VaultDynamicSecret'})
        all_docs = [d for p in (ROOT / '04-openbao/resources').glob('*.yaml')
                    for d in yaml.safe_load_all(p.read_text()) if d]
        self.assertFalse(any(d['kind'] in ('Token', 'ServiceMonitor', 'PodDisruptionBudget')
                             for d in all_docs))
        self.assertFalse(any(d['kind'] == 'Secret' and
                             d['metadata']['name'] == 'openbao-metrics' for d in all_docs))
        activation = (ROOT / '03-provider-vault/resources/activation-policy.yaml').read_text()
        self.assertNotIn('tokens.vault.vault.upbound.io', activation)

    def test_monitor_references_vso_destination(self):
        monitor = self.values['serverTelemetry']['serviceMonitor']
        self.assertTrue(monitor['enabled'])
        self.assertEqual(monitor['authorization'], {
            'type': 'Bearer', 'credentials': {'name': self.by_kind['VaultDynamicSecret']
                                            ['spec']['destination']['name'], 'key': 'token'}})
        self.assertNotIn('bearerTokenFile', monitor)

    def test_lifecycle_evidence_and_rollout_prerequisite_are_documented(self):
        text = (ROOT / 'docs/service-credentials.md').read_text()
        for source in ('v1.6.0/vault/client.go', 'v1.6.0/vault/client_factory.go',
                       'v1.6.0/vault/responses.go', 'v1.6.0/controllers/vaultdynamicsecret_controller.go',
                       'v2.7.1/internal/vault/token_store.go', 'v4.0.5/apis/cluster/kubernetes/v1alpha1/zz_authbackendrole_types.go'):
            self.assertIn(source, text)
        for phrase in ('Bootstrap prerequisite', 'before these', 'max-TTL re-login',
                       'not validated against a running cluster'):
            self.assertIn(phrase, text)
        self.assertNotIn('issuance blocked', text)

    def test_bootstrap_exact_metrics_acl_when_sibling_checkout_exists(self):
        policy = (ROOT.parent / 'k3s-on-omv-bootstrap/ansible/roles/openbao_setup'
                  '/files/crossplane-provider-vault.hcl')
        if not policy.exists():
            self.skipTest('requires sibling Bootstrap checkout')
        text = policy.read_text()
        blocks = re.findall(r'path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^]]+)\]\s*\}', text)
        metrics = [(path, re.findall(r'"([^"]+)"', caps))
                   for path, caps in blocks if 'openbao-metrics' in path]
        self.assertCountEqual(metrics, [
            ('sys/policies/acl/openbao-metrics', ['create', 'read', 'update']),
            ('auth/kubernetes/role/openbao-metrics', ['create', 'read', 'update']),
        ])

    @unittest.skipUnless(VSO_CHART.exists(), 'requires cached pinned VSO chart')
    def test_pinned_dynamic_secret_schema_supports_nonleased_get(self):
        crd = yaml.safe_load((VSO_CHART / 'crds/secrets.hashicorp.com_vaultdynamicsecrets.yaml').read_text())
        schema = next(v for v in crd['spec']['versions'] if v['name'] == 'v1beta1')
        props = schema['schema']['openAPIV3Schema']['properties']['spec']['properties']
        self.assertIn('GET', props['requestHTTPMethod']['enum'])
        self.assertIn('does not provide a lease TTL', props['refreshAfter']['description'])
        self.assertTrue(set(self.by_kind['VaultDynamicSecret']['spec']).issubset(props))

    @unittest.skipUnless(CHART.exists() and shutil.which('helm'),
                         'requires cached exact chart and Helm')
    def test_exact_chart_single_pdb_and_authenticated_monitor(self):
        self.assertEqual(self.app['spec']['sources'][0]['targetRevision'], '0.30.4')
        with tempfile.TemporaryDirectory() as directory:
            values = pathlib.Path(directory) / 'values.yaml'
            values.write_text(yaml.safe_dump(self.values))
            result = subprocess.run([
                'helm', 'template', 'openbao', str(CHART), '-n', 'openbao',
                '-f', str(values), '--api-versions', 'gateway.networking.k8s.io/v1',
                '--api-versions', 'monitoring.coreos.com/v1'],
                capture_output=True, text=True, check=True)
        docs = [d for d in yaml.safe_load_all(result.stdout) if d]
        pdbs = [d for d in docs if d['kind'] == 'PodDisruptionBudget']
        self.assertEqual(len(pdbs), 1)
        self.assertEqual(pdbs[0]['spec']['maxUnavailable'], 1)
        self.assertEqual(pdbs[0]['spec']['selector']['matchLabels']['component'], 'server')
        monitors = [d for d in docs if d['kind'] == 'ServiceMonitor']
        self.assertEqual(len(monitors), 1)
        self.assertEqual(monitors[0]['metadata']['namespace'], 'openbao')
        self.assertEqual(monitors[0]['spec']['namespaceSelector']['matchNames'], ['openbao'])
        self.assertEqual(monitors[0]['spec']['selector']['matchLabels']['openbao-active'], 'true')
        endpoint, = monitors[0]['spec']['endpoints']
        self.assertEqual(endpoint['path'], '/v1/sys/metrics')
        self.assertEqual(endpoint['authorization'], self.values['serverTelemetry']['serviceMonitor']['authorization'])
        self.assertNotIn('bearerTokenFile', endpoint)
        configs = [d for d in docs if d['kind'] == 'ConfigMap']
        self.assertTrue(any('prometheus_retention_time = "30s"' in str(d) for d in configs))


if __name__ == '__main__':
    unittest.main()
