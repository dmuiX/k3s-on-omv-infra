"""Preserve the existing platform alert identity during chart handoff.

Run with the Ansible Python. PLATFORM_CHART_AUDIT overrides the chart cache.
Chart output is captured and never printed; these tests do not access a cluster.
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
        metadata = yaml.safe_load((cls.chart / 'Chart.yaml').read_text())
        if metadata['version'] != '92.10.1':
            raise AssertionError('Cached chart must be version 92.10.1')

    def test_existing_rule_identity_and_complete_spec_remain_desired(self):
        rule = yaml.safe_load(
            (ROOT / '09-kube-prometheus-stack/resources/alerts.yaml').read_text())
        self.assertEqual(rule, self.old)
        self.assertEqual(rule['metadata']['name'], 'platform-required')
        self.assertEqual(len(rule['spec']['groups'][0]['rules']), 6)
        self.assertTrue(any(s.get('path') == '09-kube-prometheus-stack/resources'
                            for s in self.app['spec']['sources']))

    def test_chart_does_not_create_a_duplicate_platform_rule(self):
        self.assertEqual(self.source['chart'], 'kube-prometheus-stack')
        self.assertEqual(self.source['targetRevision'], '92.10.1')
        self.assertNotIn('platform-required', self.values.get('additionalPrometheusRulesMap', {}))
        command = ['helm', 'template', self.app['metadata']['name'], str(self.chart),
                   '--namespace', self.app['spec']['destination']['namespace'],
                   '--api-versions', 'monitoring.coreos.com/v1', '-f', '-']
        result = subprocess.run(command, input=yaml.safe_dump(self.values),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, 'Pinned chart render failed; output withheld')
        documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        duplicates = [doc for doc in documents if doc['kind'] == 'PrometheusRule'
                      and any(g['name'] == 'platform.required'
                              for g in doc['spec'].get('groups', []))]
        self.assertEqual(len(duplicates), 0, 'The existing standalone rule must be the only copy')
        prometheus, = [doc for doc in documents if doc['kind'] == 'Prometheus']
        self.assertEqual(prometheus['spec']['ruleSelector'], {})
        self.assertEqual(prometheus['spec']['ruleNamespaceSelector'], {})
        self.assertEqual(prometheus['metadata']['namespace'], self.old['metadata']['namespace'])
        self.assertIs(self.app['spec']['syncPolicy']['automated']['prune'], False)


if __name__ == '__main__':
    unittest.main()
