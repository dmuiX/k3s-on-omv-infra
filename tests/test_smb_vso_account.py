"""One focused exact-chart/Live contract for SMB reader-account reuse."""
import unittest

import yaml

from test_native_chart_migrations import INFRA, kind, load, render


class SMBReaderAccount(unittest.TestCase):
    def test_native_controller_account_replaces_only_the_smb_reader(self):
        app = load(INFRA / '02-controllers/csi-driver-smb/app.yml')
        rendered = render(app)
        account = 'csi-smb-controller-sa'
        namespace = 'kube-system'
        self.assertEqual(app['spec']['sources'][0]['targetRevision'], '1.20.3')
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
        resources = INFRA / '06-platform/backup-credentials/resources'
        documents = list(yaml.safe_load_all((resources / 'credentials.yaml').read_text()))
        self.assertEqual([d['metadata']['name'] for d in kind(documents, 'ServiceAccount')], ['platform-r2-vso'])
        auth, = [d for d in kind(documents, 'VaultAuth') if d['metadata']['name'] == 'smb-vso']
        self.assertEqual(auth['metadata']['namespace'], namespace)
        self.assertEqual(auth['spec']['kubernetes'], {
            'role': 'smb-vso', 'serviceAccount': account, 'tokenExpirationSeconds': 600})
        access = list(yaml.safe_load_all((resources / 'openbao-access.yaml').read_text()))
        role, = [d for d in kind(access, 'AuthBackendRole') if d['metadata']['name'] == 'smb-vso']
        self.assertEqual(role['spec']['forProvider'], {
            'backend': 'kubernetes', 'roleName': 'smb-vso',
            'boundServiceAccountNames': [account], 'boundServiceAccountNamespaces': [namespace],
            'tokenPolicies': ['smb-vso-read'], 'tokenTtl': 3600})
        policy, = [d for d in kind(access, 'Policy') if d['metadata']['name'] == 'smb-vso-read']
        self.assertEqual(policy['spec']['forProvider']['policy'].splitlines(), [
            'path "auth/token/renew-self" { capabilities = ["update"] }',
            'path "kv/data/platform/smb" { capabilities = ["read"] }',
            'path "kv/metadata/platform/smb" { capabilities = ["read"] }'])
        r2, = [d for d in kind(access, 'AuthBackendRole') if d['metadata']['name'] == 'platform-r2-vso']
        self.assertEqual(r2['spec']['forProvider']['boundServiceAccountNames'], ['platform-r2-vso'])


if __name__ == '__main__':
    unittest.main()
