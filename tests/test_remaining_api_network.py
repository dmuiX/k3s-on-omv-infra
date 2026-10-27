"""Offline remaining API egress contracts across Infra, Apps and Live."""
import fnmatch
import os
from pathlib import Path
import subprocess
import unittest

import yaml

INFRA = Path(__file__).resolve().parents[1]
LIVE = Path(os.environ.get('PLATFORM_LIVE_REPO', INFRA.parent / 'k3s-on-omv-live'))
APPS = Path(os.environ.get('PLATFORM_APPS_REPO', INFRA.parent / 'k3s-on-omv-apps'))


def load(path):
    return yaml.safe_load(path.read_text())


def tcp(*ports):
    return [{'protocol': 'TCP', 'port': p} for p in ports]


def namespace(name):
    return {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': name}}}


def api_rules():
    return [
        {'to': [{'ipBlock': {'cidr': '10.43.0.1/32'}}], 'ports': tcp(443)},
        {'to': [{'ipBlock': {'cidr': ip}} for ip in
                ('192.168.1.2/32', '192.168.1.5/32', '192.168.1.7/32')], 'ports': tcp(6443)},
    ]


class RemainingAPINetwork(unittest.TestCase):
    def test_backup_api_policy_is_rendered_by_application(self):
        app = load(INFRA / '08-backup-validation/app.yml')
        source, = [s for s in app['spec']['sources']
                   if s.get('path') == 'infra/backup-validation']
        self.assertEqual(source['repoURL'], 'https://github.com/dmuiX/k3s-on-omv-live.git')
        self.assertNotIn('ref', source)
        self.assertFalse((LIVE / source['path'] / 'kustomization.yaml').exists())
        policy = load(LIVE / source['path'] / 'api-network-policy.yaml')
        self.assertEqual(policy['metadata']['namespace'], 'backup-validation')
        self.assertEqual(policy['spec'], {
            'podSelector': {}, 'policyTypes': ['Egress'], 'egress': api_rules()})

    def test_backup_non_api_rules_are_unchanged(self):
        docs = list(yaml.safe_load_all(
            (INFRA / '08-backup-validation/resources/network-policy.yaml').read_text()))
        egress, = [d for d in docs if d['metadata']['name'] == 'allow-backup-validation-egress']
        self.assertEqual(egress['spec']['egress'], [
            {'to': [namespace('kube-system')],
             'ports': [{'protocol': 'UDP', 'port': 53}] + tcp(53, 443)},
            {'to': [namespace('openbao')], 'ports': tcp(8200)},
            {'to': [namespace('cloudnative-pg')], 'ports': tcp(5432)},
        ])
        metrics, = [d for d in docs if d['metadata']['name'] == 'allow-backup-validation-metrics']
        self.assertEqual(metrics['spec']['ingress'], [
            {'from': [namespace('kube-prometheus-stack')], 'ports': tcp(8080)}])

    def test_live_https_exceptions_preserve_exact_targeting(self):
        for directory, resource, policy_name in (
            ('backup-validation', '08-backup-validation/resources/network-policy.yaml',
             'allow-backup-validation-egress'),
            ('openbao', '04-openbao/resources/security.yaml', 'allow-openbao-snapshot-egress'),
        ):
            with self.subTest(directory=directory):
                infra_docs = list(yaml.safe_load_all((INFRA / resource).read_text()))
                internal, = [d for d in infra_docs if d['metadata']['name'] == policy_name]
                external = load(LIVE / 'infra' / directory / 'external-https-network-policy.yaml')
                self.assertEqual(external['kind'], 'NetworkPolicy')
                self.assertEqual(external['metadata'], {
                    'name': f'allow-{directory if directory != "openbao" else "openbao-snapshot"}-external-https',
                    'namespace': internal['metadata']['namespace']})
                self.assertEqual(external['spec'], {
                    'podSelector': internal['spec']['podSelector'],
                    'policyTypes': ['Egress'], 'egress': [{'ports': tcp(443)}]})
                # Additive policies: this deliberately permits EVERY destination
                # on TCP 443, including API Service 443; exact API rules cannot
                # restrict it. It does not grant API endpoint TCP 6443.
                self.assertNotIn('to', external['spec']['egress'][0])
                for doc in infra_docs:
                    for rule in doc['spec'].get('egress', []):
                        if not rule.get('to'):
                            self.assertNotIn({'protocol': 'TCP', 'port': 443}, rule.get('ports', []))

    def test_openbao_live_directory_renders_policies_not_values(self):
        app = load(INFRA / '04-openbao/app.yml')
        live_sources = [s for s in app['spec']['sources']
                        if s['repoURL'] == 'https://github.com/dmuiX/k3s-on-omv-live.git']
        source, = live_sources
        self.assertEqual(source, {
            'repoURL': 'https://github.com/dmuiX/k3s-on-omv-live.git',
            'targetRevision': 'main', 'ref': 'live', 'path': 'infra/openbao',
            'directory': {'exclude': '*values.y*ml'}})
        directory = LIVE / source['path']
        self.assertFalse((directory / 'kustomization.yaml').exists())
        rendered = [p.name for p in directory.iterdir()
                    if p.suffix in ('.yaml', '.yml')
                    and not fnmatch.fnmatchcase(p.name, source['directory']['exclude'])]
        self.assertCountEqual(rendered, [
            'api-network-policy.yaml', 'server-api-network-policy.yaml',
            'external-https-network-policy.yaml'])
        route, = [s for s in app['spec']['sources'] if s.get('path') == 'charts/service-route']
        self.assertEqual(route['helm']['valueFiles'], ['$live/infra/openbao/route-values.yml'])
        for name in rendered:
            self.assertEqual(load(directory / name)['kind'], 'NetworkPolicy')

    def test_openbao_server_api_tuples_and_selector_are_live_owned(self):
        policy = load(LIVE / 'infra/openbao/server-api-network-policy.yaml')
        self.assertEqual(policy['apiVersion'], 'networking.k8s.io/v1')
        self.assertEqual(policy['kind'], 'NetworkPolicy')
        self.assertEqual(policy['metadata'], {
            'name': 'allow-openbao-server-api', 'namespace': 'openbao'})
        self.assertEqual(policy['spec'], {
            'podSelector': {'matchLabels': {
                'app.kubernetes.io/name': 'openbao',
                'app.kubernetes.io/instance': 'openbao',
                'component': 'server'}},
            'policyTypes': ['Egress'], 'egress': api_rules()})

    def test_openbao_injector_api_tuples_are_live_owned(self):
        docs = list(yaml.safe_load_all((INFRA / '04-openbao/resources/security.yaml').read_text()))
        injector, = [d for d in docs if d['metadata']['name'] == 'allow-openbao-agent-injector']
        policy = load(LIVE / 'infra/openbao/api-network-policy.yaml')
        self.assertEqual(policy['metadata'], {
            'name': 'allow-openbao-agent-injector-api', 'namespace': 'openbao'})
        self.assertEqual(policy['spec'], {
            'podSelector': injector['spec']['podSelector'],
            'policyTypes': ['Ingress', 'Egress'],
            'ingress': [{'from': api_rules()[1]['to'], 'ports': tcp(8080)}],
            'egress': api_rules()})
        self.assertEqual(injector['spec']['podSelector'], {
            'matchLabels': {'app.kubernetes.io/name': 'openbao-agent-injector'}})
        self.assertEqual(injector['spec']['ingress'], [
            {'from': [{'ipBlock': {'cidr': '169.254.7.127/32'}}], 'ports': tcp(8080)}])
        self.assertEqual(injector['spec']['egress'], [
            {'to': [namespace('kube-system')],
             'ports': [{'protocol': 'UDP', 'port': 53}] + tcp(53)}])
        snapshot, = [d for d in docs if d['metadata']['name'] == 'allow-openbao-snapshot-egress']
        self.assertEqual(snapshot['spec']['egress'], [
            {'to': [namespace('kube-system')],
             'ports': [{'protocol': 'UDP', 'port': 53}] + tcp(53)},
            {'to': [{'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'openbao'}}}],
             'ports': tcp(8200, 15008)}])

    def test_radar_chart_renders_exact_api_rules_and_preserves_https(self):
        app = load(APPS / 'radar/app.yml')
        source, = [s for s in app['spec']['sources'] if s.get('path') == 'charts/network-policies']
        self.assertEqual(source['helm']['valueFiles'], ['$live/apps/radar/network-values.yml'])
        result = subprocess.run([
            'helm', 'template', 'radar-network', str(INFRA / source['path']),
            '-f', str(LIVE / 'apps/radar/network-values.yml')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, 'Network chart render failed')
        policies = {d['metadata']['name']: d for d in yaml.safe_load_all(result.stdout) if d}
        credentials = policies['allow-radar-auth-provisioner-api']
        self.assertEqual(credentials['metadata']['annotations'],
                         {'argocd.argoproj.io/sync-wave': '-2'})
        self.assertEqual(credentials['spec'], {
            'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'radar-auth-provisioner'}},
            'policyTypes': ['Egress'], 'egress': api_rules()})
        radar = policies['allow-radar']
        self.assertEqual(radar['spec']['egress'][-3:], api_rules() + [{'ports': tcp(443)}])
        for policy in policies.values():
            for rule in policy['spec'].get('egress', []):
                if not rule.get('to'):
                    self.assertEqual(policy['metadata']['name'], 'allow-radar')
                    self.assertEqual(rule, {'ports': tcp(443)})

    def test_apps_helper_cutover_preserves_internal_access_without_broad_api(self):
        docs = list(yaml.safe_load_all(
            (APPS / 'radar/resources/provision-credentials.yaml').read_text()))
        policy, = [d for d in docs if d['kind'] == 'NetworkPolicy']
        self.assertEqual(policy['spec']['podSelector'], {
            'matchLabels': {'app.kubernetes.io/name': 'radar-auth-provisioner'}})
        openbao = namespace('openbao')
        openbao['podSelector'] = {'matchLabels': {'app.kubernetes.io/name': 'openbao'}}
        self.assertEqual(policy['spec']['egress'], [
            {'to': [namespace('kube-system')],
             'ports': [{'protocol': 'UDP', 'port': 53}] + tcp(53)},
            {'to': [openbao], 'ports': tcp(8200, 15008)},
            {'to': [namespace('argocd')], 'ports': tcp(443, 8080, 15008)},
        ])
        job, = [d for d in docs if d['kind'] == 'Job']
        self.assertEqual(job['spec']['template']['metadata']['labels']['app.kubernetes.io/name'],
                         'radar-auth-provisioner')


if __name__ == '__main__':
    unittest.main()
