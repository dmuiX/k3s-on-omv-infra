"""Offline guards for the certificate-first GitOps graph and local references."""
from pathlib import Path
import fnmatch
import re
import shutil
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INFRA = 'https://github.com/dmuiX/k3s-on-omv-infra.git'
LIVE = 'https://github.com/dmuiX/k3s-on-omv-live.git'


def applications():
    return {d['metadata']['name']: d for p in ROOT.glob('**/app.yml')
            for d in yaml.safe_load_all(p.read_text()) if d}


class BootstrapOrderTest(unittest.TestCase):
    def test_all_yaml_parses(self):
        for p in sorted(ROOT.rglob('*')):
            if p.suffix in ('.yml', '.yaml'):
                with self.subTest(path=str(p.relative_to(ROOT))):
                    list(yaml.safe_load_all(p.read_text()))

    def test_apps_owns_radar_identity_before_attended_membership(self):
        apps = ROOT.parent / 'k3s-on-omv-apps'
        live = ROOT.parent / 'k3s-on-omv-live'
        template = (apps / 'radar/oidc/templates/oidc.yaml.tpl').read_text()
        for kind in ('Group', 'OidcKey', 'OidcScope', 'OidcClient', 'OidcProvider'):
            self.assertIn('kind: ' + kind, template)
        self.assertIn('externalMemberEntityIds: true', template)
        self.assertEqual(template.count('deletionPolicy: Orphan'), 5)
        self.assertIn('.Values.auth.oidc.issuerURL', template)
        self.assertIn('.Values.auth.oidc.redirectURL', template)
        self.assertFalse((ROOT / '06-platform/radar-config').exists())
        self.assertFalse((live / 'apps/radar').exists())
        consumers = list(yaml.safe_load_all((ROOT / '06-platform/longhorn-config/resources/oidc-proxy.yaml').read_text()))
        deployment = next(d for d in consumers if d['kind'] == 'Deployment')
        self.assertEqual(deployment['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '20')

    @unittest.skipUnless(shutil.which('helm'), 'Helm is required for OIDC rendering')
    def test_radar_oidc_uses_live_urls_before_database_and_credential_consumers(self):
        apps = ROOT.parent / 'k3s-on-omv-apps'
        chart = apps / 'radar/oidc'
        live_values = ROOT.parent / 'k3s-on-omv-live/apps/radar-values.yml'
        values = yaml.safe_load(live_values.read_text())
        command = ['helm', 'template', 'radar-oidc', str(chart), '--values', str(live_values)]
        result = subprocess.run(command, check=True, text=True, capture_output=True, timeout=30)
        documents = list(yaml.safe_load_all(result.stdout))
        by_kind = {d['kind']: d for d in documents}
        self.assertEqual(len(documents), 5)
        self.assertEqual(by_kind['OidcClient']['spec']['forProvider']['redirectUris'],
                         [values['auth']['oidc']['redirectURL']])
        from urllib.parse import urlsplit
        self.assertEqual(by_kind['OidcProvider']['spec']['forProvider']['issuerHost'],
                         urlsplit(values['auth']['oidc']['issuerURL']).netloc)
        self.assertEqual(by_kind['OidcScope']['spec']['forProvider']['template'],
                         '{"username":{{identity.entity.name}},"groups":{{identity.entity.groups.names}}}')
        for doc in documents:
            self.assertEqual(doc['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '-20')
            self.assertEqual(doc['spec']['deletionPolicy'], 'Orphan')
        job_docs = list(yaml.safe_load_all((apps / 'radar/resources/provision-credentials.yaml').read_text()))
        for doc in job_docs:
            wave = doc['metadata']['annotations']['argocd.argoproj.io/sync-wave']
            self.assertEqual(wave, '-1' if doc['kind'] == 'Job' else '-2')
        secret_docs = list(yaml.safe_load_all((apps / 'radar/resources/credentials.yaml').read_text()))
        oidc = next(d for d in secret_docs if d['metadata']['name'] == 'radar-oidc')
        self.assertEqual(oidc['spec']['mount'], 'identity')
        self.assertEqual(oidc['spec']['requestHTTPMethod'], 'GET')
        self.assertEqual(oidc['spec']['path'], 'oidc/client/radar')
        self.assertEqual(oidc['spec']['refreshAfter'], '1h')
        self.assertNotIn('allowStaticCreds', oidc['spec'])
        self.assertNotIn('rolloutRestartTargets', oidc['spec'])
        self.assertEqual(set(oidc['spec']['destination']['transformation']['templates']),
                         {'client-id', 'client-secret'})
        for key in ('issuerURL', 'redirectURL'):
            with self.subTest(missing=key), tempfile.TemporaryDirectory() as tmp:
                incomplete = yaml.safe_load(live_values.read_text())
                incomplete['auth']['oidc'].pop(key)
                path = Path(tmp) / 'values.yaml'
                path.write_text(yaml.safe_dump(incomplete))
                failure = subprocess.run(command[:-1] + [str(path)],
                                         text=True, capture_output=True, timeout=30)
                self.assertNotEqual(failure.returncode, 0)
                self.assertIn(key + ' is required', failure.stderr)

    def test_vault_activation_allowlist_is_exact_and_covers_authored_types(self):
        crossplane = applications()['crossplane']
        values = crossplane['spec']['sources'][0]['helm']['valuesObject']
        chart_allowlist = values['provider']['defaultActivations']
        policy_path = ROOT / '02-controllers/crossplane/resources/activation-policy.yaml'
        policy = yaml.safe_load(policy_path.read_text())
        policy_allowlist = policy['spec']['activate']
        self.assertEqual(policy['metadata']['name'], 'default')
        self.assertEqual(chart_allowlist, policy_allowlist)
        self.assertEqual(len(chart_allowlist), len(set(chart_allowlist)))
        self.assertNotIn('*', chart_allowlist)
        self.assertEqual(len(chart_allowlist), 16)

        provider_documents = list(yaml.safe_load_all(
            (ROOT / '03-provider-vault/resources/provider.yaml').read_text()))
        self.assertFalse(any(d.get('kind') == 'ManagedResourceActivationPolicy'
                             for d in provider_documents))

        authored = set()
        roots = [ROOT, ROOT.parent / 'k3s-on-omv-apps']
        pattern = re.compile(r'^apiVersion:\s*([^\s]+\.vault\.upbound\.io)/[^\n]+\nkind:\s*([A-Za-z0-9]+)', re.M)
        for repository in roots:
            for path in repository.rglob('*'):
                if path.suffix not in ('.yaml', '.yml', '.tpl') or '.git' in path.parts:
                    continue
                for group, kind in pattern.findall(path.read_text()):
                    plural = kind.lower()[:-1] + 'ies' if kind.lower().endswith('policy') else kind.lower() + 's'
                    authored.add(plural + '.' + group)
        self.assertTrue(authored)
        self.assertEqual(authored - set(chart_allowlist), set(),
                         'Every authored Vault managed resource API must be explicitly activated')

    def test_argocd_excludes_unused_vault_and_generated_crossplane_caches(self):
        app = applications()['argocd-config']
        config = app['spec']['sources'][0]['helm']['valuesObject']['configs']['cm']
        exclusions = yaml.safe_load(config['resource.exclusions'])

        def excluded(group, kind):
            return any(any(fnmatch.fnmatchcase(group, pattern) for pattern in entry['apiGroups']) and
                       any(fnmatch.fnmatchcase(kind, pattern) for pattern in entry['kinds'])
                       for entry in exclusions)

        self.assertTrue(excluded('database.vault.m.upbound.io', 'SecretBackendConnection'))
        self.assertTrue(excluded('pki.vault.upbound.io', 'SecretBackend'))
        self.assertTrue(excluded('apiextensions.crossplane.io', 'ManagedResourceDefinition'))
        self.assertFalse(excluded('database.vault.upbound.io', 'SecretBackendConnection'))
        self.assertFalse(excluded('identity.vault.upbound.io', 'OidcClient'))

        for path in ROOT.glob('[0-9][0-9]-*/**/*'):
            if path.suffix not in ('.yaml', '.yml'):
                continue
            for resource in yaml.safe_load_all(path.read_text()):
                if not isinstance(resource, dict) or not resource.get('kind') or not resource.get('apiVersion'):
                    continue
                api_version = resource['apiVersion']
                group = api_version.split('/', 1)[0] if '/' in api_version else ''
                self.assertFalse(excluded(group, resource['kind']),
                                 f'Authored resource excluded from Argo cache: {path.relative_to(ROOT)}')

    def test_consumer_ownership_and_stable_database_identity(self):
        apps = applications()
        core = apps['openbao-config']['spec']['sources']
        self.assertEqual([s.get('path') for s in core], ['06-platform/openbao-config/resources'])
        docs = [d for p in (ROOT / core[0]['path']).glob('*.yaml')
                for d in yaml.safe_load_all(p.read_text()) if d]
        self.assertEqual({d['kind'] for d in docs}, {'Mount', 'Backend'})
        app = apps['longhorn-config']
        sources = [s.get('path') for s in app['spec']['sources']]
        self.assertIn('06-platform/longhorn-config/resources', sources)
        self.assertIn('infra/longhorn', sources)
        self.assertEqual(app['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '6')
        radar = yaml.safe_load((ROOT.parent / 'k3s-on-omv-apps/radar/app.yml').read_text())
        radar_sources = [s.get('path') for s in radar['spec']['sources']]
        self.assertIn('radar/oidc', radar_sources)
        self.assertIn('radar/resources', radar_sources)
        self.assertNotIn('postgresql', apps)
        app = apps['k3s-postgres']
        self.assertEqual(app['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '7')
        self.assertEqual(app['spec']['destination']['namespace'], 'cloudnative-pg')
        cluster = next(d for d in yaml.safe_load_all((ROOT / '07-k3s-postgres/resources/cluster.yaml').read_text())
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
        directory = ROOT / '08-backup-validation/resources'
        config = yaml.safe_load((directory / 'kustomization.yaml').read_text())
        self.assertIn('openbao-access.yaml', config['resources'])
        self.assertIn('postgresql-retention-access.yaml', config['resources'])

    def test_openbao_ambient_transport_preserves_peer_scope(self):
        docs = list(yaml.safe_load_all((ROOT / '04-openbao/resources/security.yaml').read_text()))
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

    def test_backup_validation_precedes_observability(self):
        apps = applications()
        self.assertEqual(apps['backup-validation']['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '8')
        self.assertEqual(apps['kube-prometheus-stack']['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '9')
        self.assertEqual(apps['backup-validation']['spec']['sources'][0]['path'],
                         '08-backup-validation/resources')
        self.assertTrue(any(source.get('path') == '09-kube-prometheus-stack/resources'
                            for source in apps['kube-prometheus-stack']['spec']['sources']))
        self.assertIsInstance(apps['kube-prometheus-stack']['spec']['sources'][0]['helm']['valuesObject'], dict)

    def test_early_dependencies_are_strictly_ordered(self):
        apps = applications()
        names = ['cert-manager', 'letsencrypt-dns01-issuer', 'istio-core', 'istio-ambient', 'argocd-config']
        waves = [int(apps[n]['metadata']['annotations']['argocd.argoproj.io/sync-wave']) for n in names]
        self.assertEqual(waves[:4], [-5, -4, -3, -2])
        self.assertTrue(all(a < b for a, b in zip(waves, waves[1:])))
        self.assertEqual(int(apps['openbao']['metadata']['annotations']['argocd.argoproj.io/sync-wave']), 4)
        self.assertEqual(int(apps['letsencrypt-dns01-credentials']['metadata']['annotations']['argocd.argoproj.io/sync-wave']), 6)

    def test_certificate_phase_has_no_openbao_or_istio_resource_dependency(self):
        apps = applications()
        early = apps['letsencrypt-dns01-issuer']['spec']['sources']
        self.assertEqual([(s['repoURL'], s.get('path')) for s in early], [
            (INFRA, '00-letsencrypt-dns01-issuer/resources'), (LIVE, 'infra/letsencrypt-dns01')])
        late = apps['letsencrypt-dns01-credentials']['spec']['sources']
        self.assertEqual(late, [{'repoURL': INFRA, 'targetRevision': 'main', 'path': '06-platform/letsencrypt-dns01-credentials/resources'}])
        docs = list(yaml.safe_load_all((ROOT / '06-platform/letsencrypt-dns01-credentials/resources/identity.yaml').read_text()))
        secret = next(d for d in docs if d['kind'] == 'VaultStaticSecret')
        self.assertEqual(secret['spec']['destination'], {'name': 'cloudflare-dns01', 'create': False})

    def test_gateway_namespace_has_one_manifest_owner(self):
        owners = []
        for p in ROOT.rglob('*.yaml'):
            for d in yaml.safe_load_all(p.read_text()):
                if isinstance(d, dict) and d.get('kind') == 'Namespace' and d.get('metadata', {}).get('name') == 'istio-gateway':
                    owners.append(str(p.relative_to(ROOT)))
        self.assertEqual(owners, ['00-letsencrypt-dns01-issuer/resources/namespace.yaml'])

    def test_root_selects_every_application_once_at_both_directory_depths(self):
        root = yaml.safe_load((ROOT / 'kustomization.yaml').read_text())
        paths = sorted(ROOT.glob('**/app.yml'))
        names = [yaml.safe_load(p.read_text())['metadata']['name'] for p in paths]
        self.assertEqual(len(names), len(set(names)), 'Argo Application names must be unique')
        self.assertTrue(any(len(p.relative_to(ROOT).parts) == 2 for p in paths))
        self.assertTrue(any(len(p.relative_to(ROOT).parts) == 3 for p in paths))
        self.assertEqual(sorted(root['resources']), sorted(str(path.relative_to(ROOT)) for path in paths))

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
