"""Phase-one source selection, workload targeting and reciprocal database/server ingress."""
import unittest

import yaml

import test_native_chart_migrations as native
from workload_contracts import selector_matches

VSO = 'vault-secrets-operator'
STACK = 'kube-prometheus-stack'
LIVE_REPO = 'https://github.com/dmuiX/k3s-on-omv-live.git'
DIRECTORY = 'infra/network-policies'
CASES = [(VSO, '02-controllers/vault-secrets-operator/app.yml', 'vso-controller.yaml',
          VSO + '-controller-manager'),
         (STACK, '09-kube-prometheus-stack/app.yml', 'grafana.yaml', STACK + '-grafana')]


class PhaseOneNetwork(unittest.TestCase):
    def test_sources_select_one_file_per_application(self):
        owners = {}
        for path in native.INFRA.glob('**/app.yml'):
            app = native.load(path)
            for source in app['spec'].get('sources', [app['spec'].get('source', {})]):
                if source.get('repoURL') == LIVE_REPO and source.get('path') == DIRECTORY:
                    filename = source['directory']['include']
                    self.assertEqual(source, {'repoURL': LIVE_REPO, 'targetRevision': 'main',
                                              'path': DIRECTORY, 'directory': {'include': filename}})
                    self.assertNotIn(filename, owners)
                    owners[filename] = app['metadata']['name']
        self.assertEqual(owners, {filename: app for app, _, filename, _ in CASES})

    def test_each_private_policy_selects_only_its_rendered_workload(self):
        for namespace, path, filename, deployment in CASES:
            with self.subTest(deployment=deployment):
                app = native.load(native.INFRA / path)
                policy = native.load(native.LIVE / DIRECTORY / filename)
                self.assertEqual(policy['metadata']['namespace'], app['spec']['destination']['namespace'])
                docs = native.render(app)
                raw = next(s['path'] for s in app['spec']['sources'] if s.get('path', '').endswith('/resources'))
                for file in (native.INFRA / raw).glob('*.yaml'):
                    docs.extend(d for d in yaml.safe_load_all(file.read_text()) if d)
                selected = [d for d in docs if d['kind'] in ('Deployment', 'DaemonSet', 'StatefulSet', 'Job')
                            and d['metadata'].get('namespace', namespace) == namespace
                            and selector_matches(policy['spec']['podSelector'],
                                                 d['spec']['template']['metadata'].get('labels', {}))]
                self.assertEqual([(d['kind'], d['metadata']['name']) for d in selected], [('Deployment', deployment)])
                labels = selected[0]['spec']['template']['metadata']['labels']
                if namespace == STACK:
                    # The existing non-recursive monitoring source must not publish a second
                    # copy (or any overlapping grant) before the dedicated file is enabled.
                    legacy, = [s for s in app['spec']['sources'] if s.get('ref') == 'live']
                    self.assertEqual(legacy['directory'], {'exclude': '*values.y*ml'})
                    for file in (native.LIVE / legacy['path']).glob('*.y*ml'):
                        if not file.match(legacy['directory']['exclude']):
                            docs.extend(d for d in yaml.safe_load_all(file.read_text()) if d)
                self.assertFalse(any(p['metadata'].get('namespace', namespace) == namespace and
                                     selector_matches(p['spec']['podSelector'], labels)
                                     for p in native.kind(docs, 'NetworkPolicy')), 'overlapping workload policy')

    def test_receivers_admit_application_and_tunnel(self):
        bao, = native.kind(native.render(native.load(native.INFRA / '04-openbao/app.yml')), 'NetworkPolicy')
        pg = next(d for d in yaml.safe_load_all((native.INFRA / '07-k3s-postgres/resources/network-policy.yaml').read_text())
                  if d['metadata']['name'] == 'allow-postgresql')
        for receiver, receiver_ns, source_ns, filename, port in [
            (bao, 'openbao', VSO, 'vso-controller.yaml', 8200),
            (pg, 'cloudnative-pg', STACK, 'grafana.yaml', 5432),
        ]:
            policy = native.load(native.LIVE / DIRECTORY / filename)
            # Use the real destination peer from Live, not just a matching port in another namespace.
            peer, = [p for r in policy['spec']['egress'] if {'protocol': 'TCP', 'port': port} in r['ports']
                     for p in r['to']]
            self.assertTrue(selector_matches(receiver['spec']['podSelector'], peer['podSelector']['matchLabels']))
            self.assertEqual(peer['namespaceSelector']['matchLabels'], {'kubernetes.io/metadata.name': receiver_ns})
            admitted = [p for rule in receiver['spec']['ingress'] if any(
                'namespaceSelector' in source and
                selector_matches(source['namespaceSelector'], {'kubernetes.io/metadata.name': source_ns}) and
                selector_matches(source.get('podSelector', {}), policy['spec']['podSelector']['matchLabels'])
                for source in rule['from']) for p in rule['ports']]
            for required in (port, 15008):
                self.assertIn({'protocol': 'TCP', 'port': required}, admitted)
