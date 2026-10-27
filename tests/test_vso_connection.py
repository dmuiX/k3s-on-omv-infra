"""Shared transport configuration, never shared authentication (offline with VSO_CHART_DIR)."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
APPS = Path(os.environ.get('PLATFORM_APPS_REPO', ROOT.parent / 'k3s-on-omv-apps'))
REF = 'vault-secrets-operator/default'
ADDRESS = 'http://openbao-active.openbao.svc.cluster.local:8200'


def documents(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


class SharedConnectionTests(unittest.TestCase):
    def test_infra_consumers_keep_isolated_identities(self):
        expected = {
            'cert-manager-dns01', 'longhorn-oidc-proxy', 'openbao-snapshot',
            'smb-vso', 'platform-r2-vso', 'postgresql-vso', 'k8up-validation-vso',
            'grafana-vso', 'alertmanager-vso', 'prometheus-k8up-vso',
        }
        auths = []
        for directory in ('06-platform', '07-k3s-postgres', '08-backup-validation',
                          '09-kube-prometheus-stack'):
            for path in (ROOT / directory).glob('**/resources/*.yaml'):
                for doc in documents(path):
                    self.assertNotIn(doc['kind'], ('VaultConnection', 'VaultAuthGlobal'), str(path))
                    if doc['kind'] == 'VaultAuth':
                        auths.append(doc)
        self.assertEqual({a['spec']['kubernetes']['role'] for a in auths}, expected)
        self.assertEqual(len(auths), len(expected))
        for auth in auths:
            spec = auth['spec']
            self.assertEqual(spec['vaultConnectionRef'], REF)
            self.assertEqual((spec['method'], spec['mount']), ('kubernetes', 'kubernetes'))
            role = spec['kubernetes']['role']
            identity = {'role': role, 'serviceAccount': role}
            if role in {'longhorn-oidc-proxy', 'smb-vso', 'platform-r2-vso',
                        'postgresql-vso', 'alertmanager-vso', 'prometheus-k8up-vso'}:
                identity['tokenExpirationSeconds'] = 600
            if role == 'cert-manager-dns01':
                identity['audiences'] = ['vault://cert-manager-dns01']
            self.assertEqual(spec, {'vaultConnectionRef': REF, 'method': 'kubernetes',
                                    'mount': 'kubernetes', 'kubernetes': identity})

    @unittest.skipUnless(APPS.is_dir(), 'Set PLATFORM_APPS_REPO to the coordinated Apps worktree')
    def test_apps_use_transport_without_merging_identity(self):
        radar = documents(APPS / 'radar/resources/credentials.yaml')
        self.assertFalse(any(d['kind'] == 'VaultConnection' for d in radar))
        auth, = [d for d in radar if d['kind'] == 'VaultAuth']
        self.assertEqual(auth['spec'], {
            'method': 'kubernetes', 'mount': 'kubernetes', 'vaultConnectionRef': REF,
            'kubernetes': {'role': 'radar-vso', 'serviceAccount': 'radar-vso'}})
        # This template contains Go expressions: inspect its unchanged identity stanza.
        text = (APPS / 'audiobookshelf/chart/templates/oidc.yaml').read_text()
        self.assertNotIn('kind: VaultConnection', text)
        self.assertIn(f'vaultConnectionRef: {REF}', text)
        self.assertIn('role: audiobookshelf-vso\n    serviceAccount: audiobookshelf-vso', text)

    @unittest.skipUnless(os.environ.get('VSO_CHART_DIR'),
                         'Set VSO_CHART_DIR to the cached upstream 1.6.0 chart')
    def test_pinned_chart_renders_single_connection_and_no_default_auth(self):
        app = yaml.safe_load((ROOT / '02-controllers/vault-secrets-operator/app.yml').read_text())
        self.assertEqual(app['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '2')
        source = app['spec']['sources'][0]
        self.assertEqual(source['targetRevision'], '1.6.0')
        values = source['helm']['valuesObject']
        self.assertEqual(values['defaultVaultConnection'], {'enabled': True, 'address': ADDRESS})
        self.assertEqual(values['defaultAuthMethod'], {'enabled': False})
        with tempfile.TemporaryDirectory(prefix='vso-connection-test-') as tmp:
            chart = os.environ['VSO_CHART_DIR']
            self.assertEqual(str(yaml.safe_load((Path(chart) / 'Chart.yaml').read_text())['version']), '1.6.0')
            config = Path(tmp) / 'values.yaml'
            config.write_text(yaml.safe_dump(values))
            result = subprocess.run(['helm', 'template', 'vault-secrets-operator', chart,
                                     '--namespace', 'vault-secrets-operator', '-f', str(config)],
                                    check=True, capture_output=True, text=True)
            rendered = [d for d in yaml.safe_load_all(result.stdout) if d]
            connection, = [d for d in rendered if d['kind'] == 'VaultConnection']
            self.assertEqual(connection['metadata']['namespace'], 'vault-secrets-operator')
            self.assertEqual(connection['metadata']['name'], 'default')
            self.assertEqual(connection['spec'], {'address': ADDRESS, 'skipTLSVerify': False})
            self.assertFalse(any(d['kind'] in ('VaultAuth', 'VaultAuthGlobal') for d in rendered))
            crds = Path(chart) / 'crds'
            schema = documents(crds / 'secrets.hashicorp.com_vaultconnections.yaml')[0]['spec']['versions'][0]['schema']['openAPIV3Schema']['properties']['spec']
            self.assertNotIn('allowedNamespaces', schema['properties'])
            self.assertLessEqual(set(connection['spec']), set(schema['properties']))
            self.assertLessEqual(set(schema['required']), set(connection['spec']))
            auth_schema = documents(crds / 'secrets.hashicorp.com_vaultauths.yaml')[0]['spec']['versions'][0]['schema']['openAPIV3Schema']['properties']['spec']['properties']
            self.assertIn('prefixed with a namespace', auth_schema['vaultConnectionRef']['description'])


if __name__ == '__main__':
    unittest.main()
