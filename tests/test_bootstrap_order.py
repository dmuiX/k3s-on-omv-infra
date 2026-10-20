"""Offline guards for the certificate-first GitOps graph and local references."""
from pathlib import Path
import re
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INFRA = 'https://github.com/dmuiX/k3s-on-omv-infra.git'
LIVE = 'https://github.com/dmuiX/k3s-on-omv-live.git'


def applications():
    return {d['metadata']['name']: d for p in ROOT.glob('*/*/app.yml')
            for d in yaml.safe_load_all(p.read_text()) if d}


class BootstrapOrderTest(unittest.TestCase):
    def test_all_yaml_parses(self):
        for p in sorted(ROOT.rglob('*')):
            if p.suffix in ('.yml', '.yaml'):
                with self.subTest(path=str(p.relative_to(ROOT))):
                    list(yaml.safe_load_all(p.read_text()))

    def test_openbao_ambient_transport_preserves_peer_scope(self):
        docs = list(yaml.safe_load_all((ROOT / '04-core/openbao/resources/security.yaml').read_text()))
        policy = next(d['spec'] for d in docs if d['metadata']['name'] == 'allow-openbao-raft-and-clients')
        hbone = [r for r in policy['ingress'] if any(p['port'] == 15008 for p in r['ports'])]
        self.assertEqual(len(hbone), 1)
        clients = next(r['from'] for r in policy['ingress'] if any('namespaceSelector' in p for p in r['from'])
                       and any(p['port'] == 8200 for p in r['ports']))
        self.assertEqual(hbone[0]['from'], [{'podSelector': {}}, *clients])
        peers = [{'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'openbao'}}}]
        egress = next(r for r in policy['egress'] if r.get('to') == peers)
        self.assertEqual({p['port'] for p in egress['ports']}, {8200, 8201, 15008})
        snapshot = next(d['spec'] for d in docs if d['metadata']['name'] == 'allow-openbao-snapshot-egress')
        self.assertEqual({p['port'] for r in snapshot['egress'] if r.get('to') == peers for p in r['ports']},
                         {8200, 15008})

    def test_early_dependencies_are_strictly_ordered(self):
        apps = applications()
        names = ['cert-manager', 'tls-certificates', 'istio-core', 'istio-ambient', 'argocd-config']
        waves = [int(apps[n]['metadata']['annotations']['argocd.argoproj.io/sync-wave']) for n in names]
        self.assertEqual(waves[:4], [-5, -4, -3, -2])
        self.assertTrue(all(a < b for a, b in zip(waves, waves[1:])))
        self.assertEqual(int(apps['openbao']['metadata']['annotations']['argocd.argoproj.io/sync-wave']), 4)
        self.assertEqual(int(apps['letsencrypt-dns01']['metadata']['annotations']['argocd.argoproj.io/sync-wave']), 6)

    def test_certificate_phase_has_no_openbao_or_istio_resource_dependency(self):
        apps = applications()
        early = apps['tls-certificates']['spec']['sources']
        self.assertEqual([(s['repoURL'], s.get('path')) for s in early], [
            (INFRA, '00-bootstrap/tls-certificates/resources'), (LIVE, 'infra/letsencrypt-dns01')])
        late = apps['letsencrypt-dns01']['spec']['sources']
        self.assertEqual(late, [{'repoURL': INFRA, 'targetRevision': 'main', 'path': '06-platform/letsencrypt-dns01/resources'}])
        docs = list(yaml.safe_load_all((ROOT / '06-platform/letsencrypt-dns01/resources/identity.yaml').read_text()))
        secret = next(d for d in docs if d['kind'] == 'VaultStaticSecret')
        self.assertEqual(secret['spec']['destination'], {'name': 'cloudflare-dns01', 'create': False})

    def test_gateway_namespace_has_one_manifest_owner(self):
        owners = []
        for p in ROOT.rglob('*.yaml'):
            for d in yaml.safe_load_all(p.read_text()):
                if isinstance(d, dict) and d.get('kind') == 'Namespace' and d.get('metadata', {}).get('name') == 'istio-gateway':
                    owners.append(str(p.relative_to(ROOT)))
        self.assertEqual(owners, ['00-bootstrap/tls-certificates/resources/namespace.yaml'])

    def test_application_schema_options_and_infra_paths(self):
        for name, app in applications().items():
            with self.subTest(app=name):
                spec = app['spec']
                policy = spec['syncPolicy']
                self.assertNotIn('syncOptions', policy.get('automated', {}))
                for option in policy.get('syncOptions', []):
                    self.assertIsInstance(option, str)
                    self.assertRegex(option, r'^[A-Za-z][A-Za-z0-9]*=\S+$')
                sources = spec.get('sources', [spec.get('source', {})])
                refs = {s['ref'] for s in sources if 'ref' in s}
                for source in sources:
                    if source.get('repoURL') == INFRA and 'path' in source:
                        self.assertTrue((ROOT / source['path']).is_dir(), source['path'])
                    for value in source.get('helm', {}).get('valueFiles', []):
                        if value.startswith('$'):
                            ref, path = value[1:].split('/', 1)
                            self.assertIn(ref, refs)
                            if ref == 'infra':
                                self.assertTrue((ROOT / path).is_file(), value)


if __name__ == '__main__':
    unittest.main()
