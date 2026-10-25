"""Native Helm defaults, private value files and legacy overlay contracts."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

INFRA = Path(__file__).resolve().parents[1]
LIVE = INFRA.parent / 'k3s-on-omv-live'
APPS = INFRA.parent / 'k3s-on-omv-apps'


class InlineHelmValuesTest(unittest.TestCase):
    def test_chart_overrides_live_in_the_application(self):
        count = 0
        for repository in (INFRA,):
            for path in repository.glob('**/app.yml'):
                app = yaml.safe_load(path.read_text())
                for source in app['spec'].get('sources', []):
                    if not source.get('chart'):
                        continue
                    helm = source.get('helm', {})
                    for value_file in helm.get('valueFiles', []):
                        self.assertTrue(value_file.startswith('$live/infra/helm-values/'), str(path))
                    self.assertFalse(helm.get('values'), str(path))
                    if source['chart'] != 'prometheus-operator-crds':
                        self.assertIsInstance(helm.get('valuesObject'), dict, str(path))
                    count += 1
        self.assertGreater(count, 10)

    def test_public_root_and_legacy_overlay_have_one_canonical_public_source(self):
        root = yaml.safe_load((INFRA / 'infra.yml').read_text())
        self.assertEqual(root['spec']['source']['repoURL'], 'https://github.com/dmuiX/k3s-on-omv-infra.git')
        self.assertEqual(root['spec']['source']['path'], '.')
        self.assertEqual(root['spec']['source']['directory'], {
            'recurse': True, 'include': '{*/app.yml,*/*/app.yml}'})
        base = yaml.safe_load((INFRA / 'kustomization.yaml').read_text())
        self.assertEqual(set(base['resources']), {str(p.relative_to(INFRA)) for p in INFRA.glob('**/app.yml')})
        overlay = yaml.safe_load((LIVE / 'infra/applications/kustomization.yaml').read_text())
        self.assertEqual(overlay['resources'], ['https://github.com/dmuiX/k3s-on-omv-infra.git//?ref=main'])
        bootstrap = (INFRA.parent / 'k3s-on-omv-bootstrap/ansible/roles/gitops_bootstrap/tasks/argocd.yml').read_text()
        self.assertIn('01-bootstrap/argocd-config/app.yml', bootstrap)
        self.assertIn('valuesObject', bootstrap)
        self.assertNotIn('/01-bootstrap/argocd-config/values.yml', bootstrap)

    @unittest.skipUnless(shutil.which('kubectl'), 'kubectl Kustomize is required')
    def test_private_patches_render_into_native_application_values(self):
        overlay_dir = LIVE / 'infra/applications'
        overlay = yaml.safe_load((overlay_dir / 'kustomization.yaml').read_text())
        expected = {
            'argocd-config': ('configs', 'cm', 'url'),
            'longhorn': ('defaultSettings', 'defaultDataPath'),
            'openbao': ('server', 'gateway', 'httpRoute', 'hosts'),
            'kube-prometheus-stack': ('grafana', 'grafana.ini', 'server', 'root_url'),
        }
        apps = [yaml.safe_load(p.read_text()) for p in INFRA.glob('**/app.yml')]
        for name, keys in expected.items():
            app = next(a for a in apps if a['metadata']['name'] == name)
            patch = next(p for p in overlay['patches'] if p['target']['name'] == name)
            with self.subTest(application=name), tempfile.TemporaryDirectory(prefix='inline-values-') as tmp:
                root = Path(tmp)
                (root / 'app.yaml').write_text(yaml.safe_dump(app, sort_keys=False))
                operations = yaml.safe_load((overlay_dir / patch['path']).read_text())
                (root / 'private.yaml').write_text(yaml.safe_dump(operations, sort_keys=False))
                (root / 'kustomization.yaml').write_text(yaml.safe_dump({
                    'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
                    'resources': ['app.yaml'], 'patches': [{**patch, 'path': 'private.yaml'}]}, sort_keys=False))
                result = subprocess.run(['kubectl', 'kustomize', str(root)], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, 'Native Kustomize overlay rendering failed')
                rendered = yaml.safe_load(result.stdout)
                values = rendered['spec']['sources'][0]['helm']['valuesObject']
                value = values
                for key in keys:
                    value = value[key]
                self.assertTrue(value)
                self.assertEqual(rendered['spec']['sources'][0]['helm']['valueFiles'],
                    [f'$live/infra/helm-values/{name}.yml'])
                for operation in operations:
                    self.assertTrue(operation['path'].startswith('/spec/sources/0/helm/valuesObject/'))

    @unittest.skipUnless(shutil.which('kubectl'), 'kubectl Kustomize is required')
    def test_full_private_overlay_builds_all_applications(self):
        directory = LIVE / 'infra/applications'
        overlay = yaml.safe_load((directory / 'kustomization.yaml').read_text())
        with tempfile.TemporaryDirectory(prefix='private-applications-') as tmp:
            root = Path(tmp).resolve()
            (root / 'patches').mkdir()
            # Resolve the same public tree locally for a network-free native build.
            # Kustomize requires a relative local base, not an absolute root.
            overlay['resources'] = [os.path.relpath(INFRA, root)]
            for patch in overlay['patches']:
                (root / patch['path']).write_bytes((directory / patch['path']).read_bytes())
            (root / 'kustomization.yaml').write_text(yaml.safe_dump(overlay, sort_keys=False))
            result = subprocess.run(['kubectl', 'kustomize', str(root),
                '--load-restrictor', 'LoadRestrictionsNone'], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, 'Full native private overlay build failed')
            applications = list(yaml.safe_load_all(result.stdout))
            expected = [yaml.safe_load(p.read_text()) for p in INFRA.glob('**/app.yml')]
            identities = lambda docs: {(d['metadata']['namespace'], d['metadata']['name']) for d in docs}
            self.assertEqual(len(applications), len(expected))
            self.assertEqual(identities(applications), identities(expected))
            self.assertEqual(len(identities(applications)), len(applications))

    def test_grafana_rollout_fits_two_nodes_and_keeps_external_database(self):
        app = yaml.safe_load((INFRA / '09-kube-prometheus-stack/app.yml').read_text())
        grafana = app['spec']['sources'][0]['helm']['valuesObject']['grafana']
        self.assertEqual(grafana['replicas'], 2)
        self.assertEqual(grafana['deploymentStrategy'], {
            'type': 'RollingUpdate',
            'rollingUpdate': {'maxSurge': 0, 'maxUnavailable': 1},
        })
        self.assertFalse(grafana['persistence']['enabled'])
        self.assertEqual(grafana['podDisruptionBudget']['minAvailable'], 1)
        self.assertTrue(grafana['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution'])
        patches = yaml.safe_load((LIVE / 'infra/applications/patches/kube-prometheus-stack.yaml').read_text())
        ini = next(p['value'] for p in patches if p['path'].endswith('/grafana/grafana.ini'))
        self.assertEqual(ini['database']['type'], 'postgres')
        self.assertEqual(ini['database']['name'], 'grafana')
        self.assertIn('$__file{', ini['database']['password'])

    def test_node_exporter_has_an_isolated_host_metrics_namespace(self):
        app = yaml.safe_load((INFRA / '09-kube-prometheus-stack/app.yml').read_text())
        exporter = app['spec']['sources'][0]['helm']['valuesObject']['prometheus-node-exporter']
        self.assertEqual(exporter['namespaceOverride'], 'node-exporter')
        self.assertEqual(exporter['resources']['requests']['memory'], '32Mi')
        self.assertEqual(exporter['resources']['limits']['memory'], '128Mi')
        self.assertFalse(exporter['containerSecurityContext']['allowPrivilegeEscalation'])
        self.assertEqual(exporter['containerSecurityContext']['capabilities']['drop'], ['ALL'])
        namespace = yaml.safe_load((INFRA / '09-kube-prometheus-stack/resources/node-exporter-namespace.yaml').read_text())
        self.assertEqual(namespace['metadata']['name'], exporter['namespaceOverride'])
        self.assertEqual(namespace['metadata']['labels']['pod-security.kubernetes.io/enforce'], 'privileged')
        monitoring = next(d for d in yaml.safe_load_all((INFRA / '09-kube-prometheus-stack/resources/grafana.yaml').read_text()) if d['kind'] == 'Namespace')
        self.assertEqual(monitoring['metadata']['labels']['pod-security.kubernetes.io/enforce'], 'restricted')

    def test_prometheus_backup_podconfig_uses_a_boolean(self):
        docs = yaml.safe_load_all((LIVE / 'infra/kube-prometheus-stack/prometheus-k8up-omv.yaml').read_text())
        config = next(d for d in docs if d['kind'] == 'PodConfig')
        self.assertIs(config['spec']['template']['spec']['securityContext']['runAsNonRoot'], True)

    def test_no_duplicate_infra_values_files_remain(self):
        # Apps are deliberately deferred; their private Radar overrides stay in Live.
        for repository in (INFRA, LIVE):
            for path in repository.glob('**/*values.y*ml'):
                if repository == LIVE and path.relative_to(LIVE).match('apps/*/values.yml'):
                    continue
                self.assertIn('tests', path.parts, str(path))


if __name__ == '__main__':
    unittest.main()
