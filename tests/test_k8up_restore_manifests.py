"""Render both Argo source paths offline. No Kubernetes context, Secrets or repositories."""
import os
from pathlib import Path
import runpy
import tempfile
import unittest

import yaml

from workload_contracts import public_path, render_kustomization

ROOT = Path(__file__).resolve().parents[1]
POD_NAME = 'k8up-restore-test'
CRON_NAME = {'backup-validation': 'k8up-backup-validation', 'kube-prometheus-stack': 'prometheus-restore-test'}
LABEL = 'app.kubernetes.io/name'
BACKUP = '08-backup-validation'
PROM = '09-kube-prometheus-stack'
TARGETS = {
    'backup-validation': (f'{BACKUP}/resources', 'k8up-omv-repository', 'k8up-repository', '256Mi'),
    'kube-prometheus-stack': (f'{PROM}/restore-test', 'prometheus-k8up-repository', 'prometheus-k8up-repository', '48Gi'),
}


def render(path):
    return {(d['kind'], d['metadata']['name']): d for d in render_kustomization(ROOT, ROOT / path)}


class RestoreTestManifests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = {ns: render(t[0]) for ns, t in TARGETS.items()}
        cls.seed = yaml.safe_load(public_path(ROOT, f'{BACKUP}/manual/seed-job.yaml').read_text())

    def test_sources_and_schedule_ownership(self):
        schedules = set()
        for ns, docs in self.docs.items():
            with self.subTest(namespace=ns):
                path = Path(TARGETS[ns][0]); app = yaml.safe_load((ROOT / path.parent / 'app.yml').read_text())
                source, = [s for s in app['spec']['sources'] if s.get('path') == str(path)]
                self.assertNotIn('helm', source)
                self.assertEqual(app['spec']['destination']['namespace'], ns)
                metadata = docs['CronJob', CRON_NAME[ns]]['metadata']
                self.assertEqual(metadata['namespace'], ns)
                cron = docs['CronJob', CRON_NAME[ns]]['spec']; job = cron['jobTemplate']['spec']
                self.assertIs(cron['suspend'], ns != 'backup-validation'); self.assertEqual(cron['timeZone'], 'Europe/Berlin')
                self.assertNotIn('argocd.argoproj.io/ignore-healthcheck',
                                 metadata.get('annotations', {}))
                self.assertEqual(cron['concurrencyPolicy'], 'Forbid')
                self.assertRegex(cron['schedule'], r'^\d+ \d+ \* \* \*$')
                schedules.add(cron['schedule'])
                for key in ('successfulJobsHistoryLimit', 'failedJobsHistoryLimit', 'startingDeadlineSeconds'):
                    self.assertGreater(cron[key], 0)
                self.assertEqual(job['backoffLimit'], 0)
                self.assertGreater(job['activeDeadlineSeconds'], 0)
                self.assertLessEqual(job['activeDeadlineSeconds'], 7200)
                self.assertGreater(job['ttlSecondsAfterFinished'], 0)
                self.assertEqual(job['template']['spec']['restartPolicy'], 'Never')
        self.assertEqual(len(schedules), 2)
        self.assertFalse((ROOT / PROM / 'resources/kustomization.yaml').exists())

    def test_scratch_and_credential_boundaries(self):
        for ns, (_, repo, secret, size) in TARGETS.items():
            with self.subTest(namespace=ns):
                docs = self.docs[ns]; pod = docs['CronJob', CRON_NAME[ns]]['spec']['jobTemplate']['spec']['template']['spec']
                vols = {v['name']: v for v in pod['volumes']}
                self.assertEqual(set(vols), {'repository', 'scratch', 'tools'})
                self.assertEqual(vols['repository']['persistentVolumeClaim'], {'claimName': repo, 'readOnly': True})
                claim = vols['scratch']['ephemeral']['volumeClaimTemplate']
                self.assertEqual(claim['metadata']['annotations']['k8up.io/backup'], 'false')
                self.assertEqual(claim['spec'], {'storageClassName': 'longhorn-single-node-delete',
                                 'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': size}}})
                tools = docs['ConfigMap', vols['tools']['configMap']['name']]['data']
                self.assertEqual(set(tools), {'restore.sh', 'verify.py', 'read-samples.sh'})
                self.assertEqual(tools['read-samples.sh'], (ROOT / 'adapters/k8up-restore/read-samples.sh').read_text())
                stages = pod['initContainers'] + pod['containers']; restore = stages[0]
                self.assertFalse(pod['automountServiceAccountToken']); self.assertFalse(pod['enableServiceLinks'])
                self.assertEqual(restore['name'], 'restore')
                self.assertEqual({e['name']: e for e in restore['env']}['RESTIC_PASSWORD']['valueFrom'],
                                 {'secretKeyRef': {'name': secret, 'key': 'password'}})
                self.assertIn({'name': 'repository', 'mountPath': '/repository', 'readOnly': True}, restore['volumeMounts'])
                for c in stages:
                    self.assertRegex(c['image'], r'@sha256:[0-9a-f]{64}$')
                    self.assertNotIn('envFrom', c); self.assertNotIn('volumeDevices', c)
                    self.assertIn('/scratch', [m['mountPath'] for m in c['volumeMounts']])
                    self.assertIn({'name': 'tools', 'mountPath': '/tools', 'readOnly': True}, c['volumeMounts'])
                    self.assertEqual(c['terminationMessagePolicy'], 'FallbackToLogsOnError')
                    self.assertTrue(c['securityContext']['readOnlyRootFilesystem'])
                    self.assertFalse(c['securityContext']['allowPrivilegeEscalation'])
                    self.assertEqual(c['securityContext']['capabilities']['drop'], ['ALL'])
                    for k in ('requests', 'limits'):
                        self.assertTrue({'cpu', 'memory'} <= c['resources'][k].keys())
                for c in stages[1:]:
                    self.assertFalse(any('valueFrom' in e for e in c.get('env', [])))
                    self.assertNotIn('repository', [m['name'] for m in c['volumeMounts']])
                if ns == 'backup-validation':
                    self.assertEqual(restore['args'], ['backup-validation', '172800'])
                    self.assertEqual([c['name'] for c in stages], ['restore', 'verify'])
                    self.assertEqual(stages[-1]['args'], ['backup-validation'])
                else:
                    self.assertEqual(pod['nodeSelector'], {'monitoring': 'true'})
                    self.assertEqual(restore['args'], ['prometheus-stream', '691200'])
                    self.assertEqual([c['name'] for c in stages], ['restore', 'validate-archive', 'verify'])
                    self.assertEqual(stages[1]['command'][-2:], ['/tools/verify.py', 'prometheus'])
                    self.assertEqual(stages[-1]['command'], ['/bin/sh', '/tools/read-samples.sh'])

    def test_network_isolation_in_infra_render(self):
        for ns, docs in self.docs.items():
            job = docs['CronJob', CRON_NAME[ns]]['spec']['jobTemplate']['spec']
            pods = [job['template']]
            if ns == 'backup-validation':
                pods.append(self.seed['spec']['template'])
                for name in ('allow-backup-validation-egress', 'allow-backup-validation-metrics'):
                    selector = docs['NetworkPolicy', name]['spec']['podSelector']
                    self.assertIn({'key': LABEL, 'operator': 'NotIn', 'values': [POD_NAME]}, selector['matchExpressions'])
            for pod in pods:
                self.assertEqual(pod['metadata']['labels'][LABEL], POD_NAME)
                self.assertEqual(pod['metadata']['labels']['istio.io/dataplane-mode'], 'none')
            policy = docs['NetworkPolicy', 'isolate-k8up-restore-test']
            self.assertEqual(policy['metadata']['namespace'], ns)
            self.assertEqual(policy['spec'], {'podSelector': {'matchLabels': {LABEL: POD_NAME}},
                             'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []})

    def test_one_shot_seed_is_confined_and_non_destructive(self):
        identity = ('Job', 'k8up-canary-seed-v1')
        self.assertEqual(self.seed['kind'], identity[0])
        self.assertEqual(self.seed['metadata'], {'name': identity[1], 'namespace': 'backup-validation'})
        for rendered in self.docs.values():
            self.assertNotIn(identity, rendered)
        docs = self.docs['backup-validation']; job = self.seed['spec']
        self.assertFalse(job.get('suspend', False)); self.assertEqual(job['ttlSecondsAfterFinished'], 3600)
        self.assertEqual(job['backoffLimit'], 0); self.assertEqual(job['activeDeadlineSeconds'], 300)
        pod = job['template']['spec']; self.assertFalse(pod['automountServiceAccountToken'])
        fixture, tool = pod['volumes']; self.assertEqual(fixture['persistentVolumeClaim'], {'claimName': 'k8up-validation-canary'})
        pvc = docs['PersistentVolumeClaim', 'k8up-validation-canary']; self.assertEqual(pvc['spec']['resources']['requests']['storage'], '128Mi')
        self.assertEqual(pvc['metadata']['annotations']['k8up.io/backup'], 'true')
        self.assertNotIn('env', pod['containers'][0])
        script = ROOT / BACKUP / 'restore-test/seed.py'
        cm = docs['ConfigMap', tool['configMap']['name']]
        self.assertEqual(cm['metadata']['namespace'], self.seed['metadata']['namespace'])
        self.assertIs(cm['immutable'], True)
        self.assertEqual(cm['data'], {'seed.py': script.read_text()})
        seed = runpy.run_path(str(script))['seed']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); marker = root / 'marker.txt'; other = root / 'other'
            seed(root); original = marker.stat(); seed(root)
            self.assertEqual(marker.read_bytes(), b'k8up-validation-canary:v1\n')
            self.assertEqual(marker.stat().st_mtime_ns, original.st_mtime_ns)
            other.write_text('leave intact'); marker.unlink(); marker.symlink_to(other)
            with self.assertRaises(OSError):
                seed(root)
            marker.unlink(); os.link(other, marker)
            with self.assertRaises(ValueError):
                seed(root)
            self.assertEqual(other.read_text(), 'leave intact'); marker.unlink(); other.rename(marker)
            with self.assertRaises(ValueError):
                seed(root)
            self.assertEqual(marker.read_text(), 'leave intact')


if __name__ == '__main__':
    unittest.main()
