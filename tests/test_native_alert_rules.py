"""Offline pinned-chart alert equivalence and Prometheus discovery contracts.

Run with the Ansible Python. PLATFORM_CHART_AUDIT overrides the chart cache.
Only the additional rules and Prometheus templates are rendered; no cluster access.
"""
import os
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHARTS = Path(os.environ.get('PLATFORM_CHART_AUDIT', '/tmp/platform-chart-audit'))


class NativeAlertRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = yaml.safe_load((ROOT / '09-kube-prometheus-stack/app.yml').read_text())
        cls.source = cls.app['spec']['sources'][0]
        cls.values = cls.source['helm']['valuesObject']
        cls.old = yaml.safe_load(
            (ROOT / 'tests/fixtures/platform-required-prometheusrule.yaml').read_text())
        cls.chart = CHARTS / 'kube-prometheus-stack-92.10.1/kube-prometheus-stack'
        if not cls.chart.is_dir():
            raise AssertionError(f'Exact chart not available: {cls.chart}')
        chart_metadata = yaml.safe_load((cls.chart / 'Chart.yaml').read_text())
        if chart_metadata['version'] != '92.10.1':
            raise AssertionError('Cached chart must be version 92.10.1')

    def render(self, template):
        command = ['helm', 'template', self.app['metadata']['name'], str(self.chart),
                   '--namespace', self.app['spec']['destination']['namespace'],
                   '--api-versions', 'monitoring.coreos.com/v1',
                   '--show-only', template, '-f', '-']
        result = subprocess.run(command, input=yaml.safe_dump(self.values),
                                text=True, capture_output=True)
        # Never print captured output: unrelated templates may contain Secrets.
        self.assertEqual(result.returncode, 0, 'Targeted helm template failed')
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def test_native_rule_preserves_complete_retired_spec(self):
        self.assertEqual(self.source['chart'], 'kube-prometheus-stack')
        self.assertEqual(self.source['targetRevision'], '92.10.1')
        rules = self.values['additionalPrometheusRulesMap']
        self.assertEqual(set(rules), {'platform-required'})
        self.assertEqual(rules['platform-required'], self.old['spec'])
        self.assertEqual(self.old['metadata']['name'], 'platform-required')
        self.assertFalse((ROOT / '09-kube-prometheus-stack/resources/alerts.yaml').exists())
        rule, = self.render('templates/prometheus/additionalPrometheusRules.yaml')
        self.assertEqual(rule['apiVersion'], self.old['apiVersion'])
        self.assertEqual(rule['kind'], 'PrometheusRule')
        self.assertEqual(rule['spec'], self.old['spec'])
        self.assertEqual(rule['metadata']['name'], 'kube-prometheus-stack-platform-required')
        self.assertEqual(rule['metadata']['namespace'], self.old['metadata']['namespace'])
        self.assertEqual(rule['metadata']['labels'], {
            'app': 'kube-prometheus-stack',
            'app.kubernetes.io/managed-by': 'Helm',
            'app.kubernetes.io/instance': 'kube-prometheus-stack',
            'app.kubernetes.io/version': '92.10.1',
            'app.kubernetes.io/part-of': 'kube-prometheus-stack',
            'chart': 'kube-prometheus-stack-92.10.1',
            'release': 'kube-prometheus-stack',
            'heritage': 'Helm',
        })

    def test_rendered_prometheus_discovers_native_rule_and_old_copy(self):
        # Check the real rendered selectors, not just matching a release label:
        # false renders {}, which also selects the unlabeled retired resource.
        self.assertIs(self.values['prometheus']['prometheusSpec']['ruleSelectorNilUsesHelmValues'], False)
        prometheus, = self.render('templates/prometheus/prometheus.yaml')
        rule, = self.render('templates/prometheus/additionalPrometheusRules.yaml')
        self.assertEqual(prometheus['kind'], 'Prometheus')
        self.assertEqual(prometheus['spec']['ruleSelector'], {})
        self.assertEqual(prometheus['spec']['ruleNamespaceSelector'], {})
        self.assertEqual(prometheus['metadata']['namespace'], rule['metadata']['namespace'])
        self.assertEqual(rule['metadata']['namespace'], self.old['metadata']['namespace'])
        self.assertIs(self.app['spec']['syncPolicy']['automated']['prune'], False)


if __name__ == '__main__':
    unittest.main()
