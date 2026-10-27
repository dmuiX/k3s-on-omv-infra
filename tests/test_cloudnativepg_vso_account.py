"""One focused exact-chart contract for PostgreSQL reader-account reuse."""
import unittest

import yaml

from test_native_chart_migrations import INFRA, kind, load, render


class CloudNativePGReaderAccount(unittest.TestCase):
    def test_native_controller_account_is_the_existing_secret_reader(self):
        app = load(INFRA / '02-controllers/cloudnative-pg/app.yml')
        rendered = render(app)
        account = 'cloudnative-pg'
        namespace = 'cloudnative-pg'
        self.assertEqual(app['spec']['sources'][0]['targetRevision'], '0.29.1')
        self.assertTrue(any(d['metadata']['name'] == account for d in kind(rendered, 'ServiceAccount')))
        deployment, = kind(rendered, 'Deployment')
        self.assertEqual(deployment['spec']['template']['spec']['serviceAccountName'], account)
        bound_roles = {d['roleRef']['name'] for d in kind(rendered, 'ClusterRoleBinding')
                       if any(s.get('kind') == 'ServiceAccount' and s['name'] == account
                              and s.get('namespace') == namespace for s in d.get('subjects', []))}
        rules = [r for d in kind(rendered, 'ClusterRole') if d['metadata']['name'] in bound_roles
                 for r in d.get('rules', [])]
        self.assertTrue(any('' in r.get('apiGroups', []) and 'secrets' in r.get('resources', [])
                            and 'get' in r.get('verbs', []) and not r.get('resourceNames')
                            for r in rules), 'Native controller already reads destination Secrets')
        resources = INFRA / '07-k3s-postgres/resources'
        documents = list(yaml.safe_load_all((resources / 'credentials.yaml').read_text()))
        self.assertFalse(kind(documents, 'ServiceAccount'))
        auth, = kind(documents, 'VaultAuth')
        self.assertEqual(auth['metadata']['namespace'], namespace)
        self.assertEqual(auth['spec']['kubernetes'], {
            'role': 'postgresql-vso', 'serviceAccount': account, 'tokenExpirationSeconds': 600})
        access = list(yaml.safe_load_all((resources / 'openbao-access.yaml').read_text()))
        role, = kind(access, 'AuthBackendRole')
        self.assertEqual(role['spec']['forProvider'], {
            'backend': 'kubernetes', 'roleName': 'postgresql-vso',
            'boundServiceAccountNames': [account], 'boundServiceAccountNamespaces': [namespace],
            'tokenPolicies': ['postgresql-vso-read', 'platform-r2-read'], 'tokenTtl': 3600})
        policy, = kind(access, 'Policy')
        self.assertEqual(policy['spec']['forProvider']['policy'].splitlines(), [
            'path "auth/token/renew-self" { capabilities = ["update"] }',
            'path "kv/data/postgresql/openbao-manager" { capabilities = ["read"] }',
            'path "kv/metadata/postgresql/*" { capabilities = ["read"] }'])
        self.assertEqual({d['spec']['destination']['name'] for d in kind(documents, 'VaultStaticSecret')},
                         {'openbao-manager-credentials', 'postgresql-r2-credentials'})


if __name__ == '__main__':
    unittest.main()
