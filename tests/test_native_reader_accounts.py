"""Focused native-account contracts; only changed readers require chart renders."""
import unittest

import yaml

from test_native_chart_migrations import INFRA, kind, load, render


class NativeReaderAccounts(unittest.TestCase):
    def test_provider_account_is_managed_by_native_runtime_config(self):
        documents = list(yaml.safe_load_all(
            (INFRA / '03-provider-vault/resources/provider.yaml').read_text()))
        self.assertFalse(kind(documents, 'ServiceAccount'))
        config, = kind(documents, 'DeploymentRuntimeConfig')
        self.assertEqual(config['spec']['serviceAccountTemplate']['metadata']['name'],
                         'provider-vault')
        pod = config['spec']['deploymentTemplate']['spec']['template']['spec']
        self.assertNotIn('serviceAccountName', pod,
                         'Let Crossplane bind the account it creates from the template')
        self.assertTrue(pod.get('automountServiceAccountToken', True))
        provider, = kind(documents, 'Provider')
        self.assertEqual(provider['spec']['runtimeConfigRef']['name'], config['metadata']['name'])

    def test_vso_readers_reuse_native_workload_accounts(self):
        cases = (
            ('02-controllers/cert-manager', '06-platform/letsencrypt-dns01-credentials',
             'identity.yaml', 'cert-manager-dns01', 'cert-manager', 'Deployment',
             'cert-manager/cloudflare', 'vault://cert-manager-dns01'),
            ('09-kube-prometheus-stack', '09-kube-prometheus-stack',
             'alertmanager-email.yaml', 'alertmanager-vso',
             'kube-prometheus-stack-alertmanager', 'Alertmanager',
             'monitoring/alertmanager-config', None),
        )
        for app_path, owner, filename, role_name, account, workload_kind, path, audience in cases:
            with self.subTest(reader=role_name):
                app = load(INFRA / app_path / 'app.yml')
                namespace = app['spec']['destination']['namespace']
                rendered = render(app)
                service_account, = [d for d in kind(rendered, 'ServiceAccount')
                                    if d['metadata']['name'] == account]
                self.assertEqual(service_account['metadata'].get('namespace', namespace), namespace)
                workload, = [d for d in kind(rendered, workload_kind)
                             if d['metadata']['name'] == account]
                pod = (workload['spec']['template']['spec'] if workload_kind == 'Deployment'
                       else workload['spec'])
                self.assertEqual(pod['serviceAccountName'], account)
                if workload_kind == 'Alertmanager':
                    self.assertEqual(workload['spec']['replicas'], 0,
                                     'Identity reuse must not reactivate Alertmanager')
                resources = INFRA / owner / 'resources'
                documents = list(yaml.safe_load_all((resources / filename).read_text()))
                self.assertFalse(kind(documents, 'ServiceAccount'))
                auth, = kind(documents, 'VaultAuth')
                self.assertEqual(auth['metadata']['namespace'], namespace)
                self.assertEqual(auth['spec']['kubernetes']['serviceAccount'], account)
                self.assertEqual(auth['spec']['kubernetes']['role'], role_name)
                self.assertEqual(auth['spec']['kubernetes'].get('audiences'),
                                 [audience] if audience else None)
                access = list(yaml.safe_load_all((resources / 'openbao-access.yaml').read_text()))
                role, = [d['spec']['forProvider'] for d in kind(access, 'AuthBackendRole')
                         if d['metadata']['name'] == role_name]
                self.assertEqual(role['boundServiceAccountNames'], [account])
                self.assertEqual(role['boundServiceAccountNamespaces'], [namespace])
                self.assertEqual(role['roleName'], role_name)
                self.assertEqual(role.get('audience'), audience)
                self.assertEqual(role['tokenPolicies'], [role_name + '-read'])
                policy, = [d['spec']['forProvider']['policy'] for d in kind(access, 'Policy')
                           if d['metadata']['name'] == role_name + '-read']
                self.assertEqual(policy.splitlines(), [
                    'path "auth/token/renew-self" { capabilities = ["update"] }',
                    'path "kv/data/' + path + '" { capabilities = ["read"] }',
                    'path "kv/metadata/' + path + '" { capabilities = ["read"] }'])


if __name__ == '__main__':
    unittest.main()
