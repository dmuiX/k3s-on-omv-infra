"""Grafana admin custody contracts; render only the cached, exact native chart."""
import contextlib
import copy
import io
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

import yaml

from test_native_chart_migrations import CHARTS, render
from test_service_credentials import DOCUMENTS, INFRA, module


class GrafanaAdmin(unittest.TestCase):
    def test_native_chart_and_sidecars_use_only_vso_admin_secret(self):
        app = yaml.safe_load((INFRA / '09-kube-prometheus-stack/app.yml').read_text())
        values = copy.deepcopy(app['spec']['sources'][0]['helm']['valuesObject'])
        # Exercise both sidecars, even if site values disable one of them.
        values['grafana']['sidecar'] = {
            'dashboards': {'enabled': True}, 'datasources': {'enabled': True}}
        chart = CHARTS / 'kube-prometheus-stack-92.10.1/kube-prometheus-stack'
        self.assertTrue(chart.is_dir(), 'Exact cached chart is required')
        result = subprocess.run(['helm', 'template', 'kube-prometheus-stack', str(chart),
                                 '--namespace', 'kube-prometheus-stack', '-f', '-'],
                                input=yaml.safe_dump(values), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, 'Native chart render failed; output withheld')
        documents = [d for d in yaml.safe_load_all(result.stdout) if d]
        self.assertFalse(any(d['kind'] == 'Secret' and d['metadata']['name'] in
                             ('grafana-admin', 'kube-prometheus-stack-grafana') for d in documents),
                         'Neither admin Secret may be chart-owned')
        deployment, = [d for d in documents if d['kind'] == 'Deployment'
                       and d['metadata']['name'] == 'kube-prometheus-stack-grafana']
        containers = deployment['spec']['template']['spec']['containers']
        for name, keys in {
            'grafana': {'GF_SECURITY_ADMIN_USER': 'admin-user',
                        'GF_SECURITY_ADMIN_PASSWORD': 'admin-password'},
            'grafana-sc-dashboard': {'REQ_USERNAME': 'admin-user', 'REQ_PASSWORD': 'admin-password'},
            'grafana-sc-datasources': {'REQ_USERNAME': 'admin-user', 'REQ_PASSWORD': 'admin-password'},
        }.items():
            container, = [c for c in containers if c['name'] == name]
            env = {e['name']: e for e in container['env']}
            for variable, key in keys.items():
                self.assertEqual(env[variable]['valueFrom']['secretKeyRef'],
                                 {'name': 'grafana-admin', 'key': key})
        self.assertIn('adapters/credential-custody', [s.get('path') for s in app['spec']['sources']])

    def test_chart_service_account_is_reused_for_read_only_vso_not_provisioning(self):
        app = yaml.safe_load((INFRA / '09-kube-prometheus-stack/app.yml').read_text())
        rendered = render(app)
        deployment, = [d for d in rendered if d['kind'] == 'Deployment'
                       and d['metadata']['name'] == 'kube-prometheus-stack-grafana']
        account = deployment['spec']['template']['spec']['serviceAccountName']
        self.assertEqual(account, 'kube-prometheus-stack-grafana')
        self.assertEqual(len([d for d in rendered if d['kind'] == 'ServiceAccount'
                              and d['metadata']['name'] == account]), 1)
        resources = [d for path in (INFRA / '09-kube-prometheus-stack/resources').glob('*.yaml')
                     for d in yaml.safe_load_all(path.read_text()) if d]
        self.assertFalse(any(d['kind'] == 'ServiceAccount' and d['metadata']['name']
                             in ('grafana-vso', account) for d in resources))
        auth, = [d for d in resources if d['kind'] == 'VaultAuth'
                 and d['metadata']['name'] == 'grafana-vso']
        reader, = [d for d in resources if d['kind'] == 'AuthBackendRole'
                   and d['metadata']['name'] == 'grafana-vso']
        self.assertEqual(auth['spec']['kubernetes']['serviceAccount'], account)
        self.assertEqual(reader['spec']['forProvider']['boundServiceAccountNames'], [account])
        self.assertEqual(reader['spec']['forProvider']['boundServiceAccountNamespaces'],
                         [app['spec']['destination']['namespace']])
        self.assertEqual(reader['spec']['forProvider']['tokenPolicies'], ['grafana-vso-read'])
        job, = [d for d in DOCUMENTS['grafana'] if d['kind'] == 'Job']
        producer, = [d for d in DOCUMENTS['grafana'] if d['kind'] == 'AuthBackendRole']
        self.assertEqual(job['spec']['template']['spec']['serviceAccountName'], 'grafana-admin-provisioner')
        self.assertEqual(producer['spec']['forProvider']['boundServiceAccountNames'], ['grafana-admin-provisioner'])
        self.assertNotEqual(account, 'grafana-admin-provisioner')
        policy, = [d for d in DOCUMENTS['grafana'] if d['kind'] == 'NetworkPolicy']
        self.assertEqual(policy['spec']['podSelector']['matchLabels'],
                         {'app.kubernetes.io/name': 'grafana-admin-provisioner'})

    def test_delivery_permissions_and_bootstrap_are_narrow(self):
        documents = list(yaml.safe_load_all((INFRA / '09-kube-prometheus-stack/resources/grafana.yaml').read_text()))
        secret, = [d for d in documents if d['kind'] == 'VaultStaticSecret']
        spec = secret['spec']
        self.assertEqual((spec['mount'], spec['type'], spec['path']),
                         ('kv', 'kv-v2', 'monitoring/grafana-admin'))
        self.assertEqual(spec['vaultAuthRef'], 'grafana-vso')
        self.assertEqual(spec['destination']['name'], 'grafana-admin')
        self.assertTrue(spec['destination']['create'])
        self.assertTrue(spec['destination']['transformation']['excludeRaw'])
        self.assertEqual(spec['destination']['transformation']['includes'],
                         ['^admin-user$', '^admin-password$'])
        reader = next(d for d in documents if d['metadata']['name'] == 'grafana-vso-read')
        self.assertEqual(reader['spec']['forProvider']['policy'].splitlines(), [
            'path "auth/token/renew-self" { capabilities = ["update"] }',
            'path "database/static-creds/grafana" { capabilities = ["read"] }',
            'path "kv/data/monitoring/grafana-admin" { capabilities = ["read"] }'])
        self.assertNotIn('rolloutRestartTargets', spec, 'No recurring password reset or rollout')
        producer = next(d for d in DOCUMENTS['grafana'] if d['kind'] == 'Policy')
        self.assertEqual(producer['spec']['forProvider']['policy'].splitlines(), [
            'path "kv/data/monitoring/grafana-admin" { capabilities = ["read", "create"] }',
            'path "kv/metadata/monitoring/grafana-admin" { capabilities = ["read"] }'])
        role = next(d for d in DOCUMENTS['grafana'] if d['kind'] == 'AuthBackendRole')
        job = next(d for d in DOCUMENTS['grafana'] if d['kind'] == 'Job')
        account = next(d for d in DOCUMENTS['grafana'] if d['kind'] == 'ServiceAccount')
        self.assertEqual(job['metadata']['name'], 'grafana-admin-provisioner')
        self.assertEqual(account['metadata']['name'], job['metadata']['name'])
        self.assertEqual(job['spec']['template']['spec']['serviceAccountName'], account['metadata']['name'])
        self.assertEqual(role['spec']['forProvider']['boundServiceAccountNames'], [account['metadata']['name']])
        self.assertEqual(role['spec']['forProvider']['audience'], 'vault://grafana-admin-provisioner')
        self.assertEqual({d['metadata']['name'] for d in DOCUMENTS['grafana']}, {'grafana-admin-provisioner'})
        self.assertEqual(producer['spec']['forProvider']['name'], 'grafana-admin-provisioner')
        self.assertEqual(role['spec']['forProvider']['roleName'], 'grafana-admin-provisioner')
        self.assertEqual(role['spec']['forProvider']['tokenPolicies'], ['grafana-admin-provisioner'])
        self.assertIn('login("grafana-admin-provisioner")', next(d['data']['provision.py']
                      for d in DOCUMENTS['grafana'] if d['kind'] == 'ConfigMap'))
        network = next(d for d in DOCUMENTS['grafana'] if d['kind'] == 'NetworkPolicy')
        self.assertEqual(network['spec']['podSelector']['matchLabels'],
                         {'app.kubernetes.io/name': 'grafana-admin-provisioner'})
        self.assertEqual(job['spec']['template']['metadata']['labels']['app.kubernetes.io/name'],
                         'grafana-admin-provisioner')
        pod = job['spec']['template']['spec']
        self.assertEqual([v['configMap']['name'] for v in pod['volumes'] if 'configMap' in v],
                         ['grafana-admin-provisioner', 'credential-custody'])
        self.assertEqual([s['serviceAccountToken']['audience'] for v in pod['volumes']
                          for s in v.get('projected', {}).get('sources', []) if 'serviceAccountToken' in s],
                         ['vault://grafana-admin-provisioner'])
        self.assertEqual(role['spec']['forProvider']['boundServiceAccountNamespaces'], ['kube-prometheus-stack'])
        policy = (INFRA.parent / 'k3s-on-omv-bootstrap/ansible/roles/openbao_setup/files/crossplane-provider-vault.hcl').read_text()
        for path in ('sys/policies/acl/grafana-admin-provisioner', 'auth/kubernetes/role/grafana-admin-provisioner'):
            self.assertIn(f'path "{path}" {{ capabilities = ["create", "read", "update"] }}', policy)
        self.assertNotIn('kv/data/monitoring/grafana-admin', policy,
                         'Provider must not gain access to the credential value')

    def test_entrypoint_reuses_custody_and_fails_closed_without_logging_password(self):
        helper = module()
        entrypoint = types.ModuleType('grafana_provision')
        script = next(d['data']['provision.py'] for d in DOCUMENTS['grafana'] if d['kind'] == 'ConfigMap')
        with patch.dict(sys.modules, {'credential_helpers': helper}):
            exec(compile(script, '<grafana-provision>', 'exec'), entrypoint.__dict__)
        store = {}

        def request(base, path, token, data=None):
            self.assertEqual(token, 'test-token')
            if data is None:
                if not store:
                    raise helper.APIError(404)
                return {'data': {'data': store.copy()}}
            self.assertEqual(data['options'], {'cas': 0})
            self.assertFalse(store, 'A resumed hook must never write again')
            store.update(data['data'])
            return {}

        with patch.object(entrypoint, 'login', return_value='test-token'), \
                patch.object(helper, 'request', side_effect=request), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            entrypoint.main()
            first = store.copy()
            with patch.object(entrypoint, 'random_value', side_effect=AssertionError('No rotation')):
                entrypoint.main()
            self.assertEqual(store, first)
            self.assertEqual(store['admin-user'], 'admin')
            self.assertGreaterEqual(len(store['admin-password']), 40)
            self.assertNotIn(store['admin-password'], output.getvalue())
            store['admin-user'] = 'foreign-admin'
            with self.assertRaisesRegex(helper.Failure, 'identity'):
                entrypoint.main()
            store['admin-user'] = 'admin'
            del store['admin-password']
            with self.assertRaisesRegex(helper.Failure, 'malformed'):
                entrypoint.main()
            store['managed-by'] = 'foreign'
            with self.assertRaisesRegex(helper.Failure, 'ownership'):
                entrypoint.main()
            store.clear()
            with patch.object(helper, 'request', side_effect=[helper.APIError(404), {}]):
                with self.assertRaisesRegex(helper.Failure, 'history'):
                    entrypoint.main()


if __name__ == '__main__':
    unittest.main()
