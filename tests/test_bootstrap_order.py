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

    def test_radar_identity_precedes_clients_and_attended_membership(self):
        docs = list(yaml.safe_load_all((ROOT / '06-platform/radar/resources/identity.yaml').read_text()))
        self.assertEqual({d['kind'] for d in docs}, {'Group', 'OidcKey', 'OidcScope'})
        for doc in docs:
            self.assertEqual(doc['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '-1')
            self.assertEqual(doc['spec']['deletionPolicy'], 'Orphan')
        group = next(d for d in docs if d['kind'] == 'Group')
        self.assertTrue(group['spec']['forProvider']['externalMemberEntityIds'])
        self.assertFalse((ROOT.parent / 'k3s-on-omv-apps/radar/resources/oidc.yaml').exists())
        consumers = list(yaml.safe_load_all((ROOT / '06-platform/longhorn/resources/oidc-proxy.yaml').read_text()))
        deployment = next(d for d in consumers if d['kind'] == 'Deployment')
        self.assertEqual(deployment['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '20')

    def test_consumer_ownership_and_stable_database_identity(self):
        apps = applications()
        core = apps['openbao-config']['spec']['sources']
        self.assertEqual([s.get('path') for s in core], ['06-platform/openbao-config/resources'])
        docs = [d for p in (ROOT / core[0]['path']).glob('*.yaml')
                for d in yaml.safe_load_all(p.read_text()) if d]
        self.assertEqual({d['kind'] for d in docs}, {'Mount', 'Backend'})
        for name, folder, live_path in [('longhorn-config', 'longhorn', 'infra/longhorn'),
                                         ('radar-config', 'radar', 'apps/radar')]:
            app = apps[name]
            sources = [s.get('path') for s in app['spec']['sources']]
            self.assertIn('06-platform/' + folder + '/resources', sources)
            self.assertIn(live_path, sources)
            self.assertEqual(app['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '6')
        self.assertNotIn('postgresql', apps)
        app = apps['k3s-postgres']
        self.assertEqual(app['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '7')
        self.assertEqual(app['spec']['destination']['namespace'], 'cloudnative-pg')
        cluster = next(d for d in yaml.safe_load_all((ROOT / '07-data/k3s-postgres/resources/cluster.yaml').read_text())
                       if d['kind'] == 'Cluster')
        self.assertEqual(cluster['metadata'], {'name': 'k3s-postgres', 'namespace': 'cloudnative-pg'})
        for source in app['spec']['sources']:
            if source['repoURL'] == LIVE:
                self.assertEqual(source['path'], 'infra/k3s-postgres')

    def test_early_apps_do_not_require_the_later_backup_validation_namespace(self):
        for name, app in applications().items():
            if int(app['metadata']['annotations']['argocd.argoproj.io/sync-wave']) >= 8:
                continue
            for source in app['spec'].get('sources', []):
                if source.get('repoURL') != INFRA or not source.get('path'):
                    continue
                for path in (ROOT / source['path']).glob('*.yaml'):
                    for resource in yaml.safe_load_all(path.read_text()):
                        if isinstance(resource, dict):
                            self.assertNotEqual(resource.get('metadata', {}).get('namespace'),
                                                'backup-validation', f'{name}: {path}')

    def test_validation_kustomization_includes_its_openbao_access(self):
        directory = ROOT / '08-observability/backup-validation/resources'
        config = yaml.safe_load((directory / 'kustomization.yaml').read_text())
        self.assertIn('openbao-access.yaml', config['resources'])
        self.assertIn('postgresql-retention-access.yaml', config['resources'])

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
