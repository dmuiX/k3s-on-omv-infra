"""Offline remaining API egress contracts across Infra, Apps and Live."""
import fnmatch
import os
from pathlib import Path
import subprocess
import unittest

import yaml

from workload_contracts import render_kustomization

INFRA = Path(__file__).resolve().parents[1]
LIVE = Path(os.environ.get('PLATFORM_LIVE_REPO', INFRA.parent / 'k3s-on-omv-live'))
APPS = Path(os.environ.get('PLATFORM_APPS_REPO', INFRA.parent / 'k3s-on-omv-apps'))


NON_RESTORE_TEST_PODS = {'matchExpressions': [
    {'key': 'app.kubernetes.io/name', 'operator': 'NotIn', 'values': ['k8up-restore-test']}]}


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
            'podSelector': NON_RESTORE_TEST_PODS, 'policyTypes': ['Egress'], 'egress': api_rules()})

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

    def test_restore_validator_hbone_targets_only_source_database_instances(self):
        root = INFRA / '08-backup-validation/resources'
        job, = (d for d in yaml.safe_load_all((root / 'validation.yaml').read_text())
                if d['kind'] == 'CronJob' and d['metadata']['name'] == 'postgresql-restore-validation')
        labels = {'app.kubernetes.io/name': job['metadata']['name']}
        pod_labels = job['spec']['jobTemplate']['spec']['template']['metadata']['labels']
        self.assertEqual(pod_labels.get('app.kubernetes.io/name'), labels['app.kubernetes.io/name'])
        policy, = (d for d in yaml.safe_load_all((root / 'network-policy.yaml').read_text())
                   if d['metadata']['name'] == 'allow-restore-validator-postgresql-hbone')
        self.assertEqual(job['metadata']['namespace'], 'backup-validation')
        self.assertEqual(policy['metadata']['namespace'], job['metadata']['namespace'])
        self.assertEqual(policy['spec'], {
            'podSelector': {'matchLabels': labels}, 'policyTypes': ['Egress'],
            'egress': [{'to': [{**namespace('cloudnative-pg'), 'podSelector': {'matchLabels': {
                'cnpg.io/cluster': 'k3s-postgres', 'cnpg.io/podRole': 'instance'}}}], 'ports': tcp(15008)}],
        })

    def test_rendered_restore_instance_paths(self):
        from test_native_chart_migrations import render

        app = load(INFRA / '02-controllers/cloudnative-pg/app.yml')
        deployment, = (d for d in render(app) if d['kind'] == 'Deployment')
        operator = {**namespace(app['spec']['destination']['namespace']),
                    'podSelector': deployment['spec']['selector']}
        restore = {'matchLabels': {'cnpg.io/cluster': 'postgresql-restore-test',
                                   'cnpg.io/podRole': 'instance'}}
        validator = {'matchLabels': {'app.kubernetes.io/name': 'postgresql-restore-validation'}}

        docs = render_kustomization(INFRA, INFRA / '08-backup-validation/resources')
        job, = (d for d in docs if d['kind'] == 'CronJob' and d['metadata']['name'] == 'postgresql-restore-validation')
        labels = job['spec']['jobTemplate']['spec']['template']['metadata']['labels']
        self.assertLessEqual(validator['matchLabels'].items(), labels.items())
        self.assertEqual(job['metadata']['namespace'], 'backup-validation')
        policies = {d['metadata']['name']: d['spec'] for d in docs if d['kind'] == 'NetworkPolicy'
                    and d['metadata']['namespace'] == 'backup-validation'}
        self.assertEqual(policies.get('allow-restore-instance-ingress'), {
            'podSelector': restore, 'policyTypes': ['Ingress'], 'ingress': [
                {'from': [operator], 'ports': tcp(8000, 15008)},
                {'from': [{'podSelector': validator}], 'ports': tcp(5432, 15008)}]})
        self.assertEqual(policies.get('allow-validator-to-restore-instance'), {
            'podSelector': validator, 'policyTypes': ['Egress'],
            'egress': [{'to': [{'podSelector': restore}], 'ports': tcp(5432, 15008)}]})

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
                # The backup-validation raw Infra file does not include its Kustomize exclusion.
                selector = NON_RESTORE_TEST_PODS if directory == 'backup-validation' else internal['spec']['podSelector']
                self.assertEqual(external['spec'], {
                    'podSelector': selector,
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
        live, = [s for s in app['spec']['sources'] if s.get('ref') == 'live']
        self.assertEqual(live['path'], 'apps/radar')
        self.assertEqual(live['directory']['include'], 'network-peers.yaml')
        policies = {d['metadata']['name']: d for path in (
            APPS / 'radar/resources/network-policy.yaml',
            LIVE / 'apps/radar/network-peers.yaml',
        ) for d in yaml.safe_load_all(path.read_text()) if d}
        credentials = policies['allow-radar-auth-provisioner-api']
        self.assertEqual(credentials['metadata']['annotations'],
                         {'argocd.argoproj.io/sync-wave': '-2'})
        self.assertEqual(credentials['spec'], {
            'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'radar-auth-provisioner'}},
            'policyTypes': ['Egress'], 'egress': api_rules()})
        radar_api = policies['allow-radar-api']
        self.assertEqual(radar_api['spec']['egress'], api_rules())
        radar = policies['allow-radar']
        self.assertEqual(radar['spec']['egress'][-1:], [{'ports': tcp(443)}])
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
