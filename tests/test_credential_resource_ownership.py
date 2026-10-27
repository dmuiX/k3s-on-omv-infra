"""One static check for credential ownership moves and producer names."""
import hashlib
import json
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def documents(path):
    return [d for d in yaml.safe_load_all((ROOT / path).read_text()) if d]


class CredentialResourceOwnership(unittest.TestCase):
    def test_moves_preserve_objects_and_producers_keep_external_identities(self):
        # Canonical hashes of the complete objects on the pre-move main branch.
        for path, expected in (
            ('06-platform/backup-credentials/resources/k8up-repository-access.yaml',
             'a74fa601a754de293a158e2a6b48b3848300d7e9935d14a5a45ebfc7338ddacd'),
            ('07-k3s-postgres/resources/openbao-database-connection.yaml',
             '4ca28d3de16f42353f2035f148fd56391b2aa861311fac82a8008cfc739d9f7b'),
        ):
            doc, = documents(path)
            self.assertEqual(hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest(), expected)
        self.assertFalse(any(d['kind'] == 'SecretBackendConnection' for d in
                             documents('07-k3s-postgres/resources/credentials.yaml')))
        self.assertFalse(any(d['kind'] == 'Policy' for d in
                             documents('08-backup-validation/resources/openbao-access.yaml')))
        waves = [int(documents(p)[0]['metadata']['annotations']['argocd.argoproj.io/sync-wave'])
                 for p in ('06-platform/backup-credentials/app.yml', '08-backup-validation/app.yml',
                           '09-kube-prometheus-stack/app.yml')]
        self.assertLess(waves[0], min(waves[1:]))
        for path in ('08-backup-validation/resources/openbao-access.yaml',
                     '09-kube-prometheus-stack/resources/openbao-access.yaml'):
            self.assertTrue(any('k8up-validation-vso-read' in
                                d.get('spec', {}).get('forProvider', {}).get('tokenPolicies', [])
                                for d in documents(path)))
        for owner, old, name in (
            ('06-platform/longhorn-config', 'longhorn-credentials', 'longhorn-proxy-provisioner'),
            ('07-k3s-postgres', 'k3s-postgres-credentials', 'k3s-postgres-manager-provisioner'),
        ):
            docs = documents(owner + '/resources/provision-credentials.yaml')
            job, = [d for d in docs if d['kind'] == 'Job']
            account, = [d for d in docs if d['kind'] == 'ServiceAccount']
            role, = [d for d in docs if d['kind'] == 'AuthBackendRole']
            pod = job['spec']['template']['spec']
            self.assertEqual((job['metadata']['name'], account['metadata']['name'],
                              pod['serviceAccountName']), (name, name, name))
            self.assertEqual(role['spec']['forProvider']['boundServiceAccountNames'], [name])
            self.assertEqual(role['spec']['forProvider']['roleName'], old)
            self.assertEqual(role['spec']['forProvider']['audience'], 'vault://' + old)
            self.assertEqual(role['spec']['forProvider']['tokenPolicies'], [old])
            audiences = [s['serviceAccountToken']['audience'] for v in pod['volumes']
                         for s in v.get('projected', {}).get('sources', []) if 'serviceAccountToken' in s]
            self.assertEqual(audiences, ['vault://' + old])
            network, = [d for d in docs if d['kind'] == 'NetworkPolicy']
            labels = job['spec']['template']['metadata']['labels']
            self.assertTrue(all(labels.get(k) == v for k, v in
                                network['spec']['podSelector']['matchLabels'].items()))


if __name__ == '__main__':
    unittest.main()
