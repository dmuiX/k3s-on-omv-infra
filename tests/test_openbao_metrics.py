"""Platform adoption must not activate an intermediate OpenBao metrics design.

The dedicated listener and its protection are owned by the follow-up metrics PR.
Only cached exact-version chart rendering is used; no cluster or credentials.
"""
from pathlib import Path
import unittest

import yaml

import test_native_chart_migrations as native

ROOT = Path(__file__).resolve().parents[1]


class OpenBaoMetricsDeferred(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = native.load(ROOT / '04-openbao/app.yml')
        cls.values = native.effective_values(cls.app)
        cls.documents = native.render(cls.app)

    def test_no_monitor_listener_or_telemetry_activation(self):
        self.assertNotIn('serverTelemetry', self.values)
        server = self.values['server']
        self.assertNotIn('telemetry {', server['ha']['raft']['config'])
        self.assertNotIn('9101', server['ha']['raft']['config'])
        self.assertFalse(native.kind(self.documents, 'ServiceMonitor'))
        for resource in self.documents:
            if resource['kind'] == 'Service':
                self.assertNotIn(9101, [port['port'] for port in resource['spec']['ports']])

    def test_no_metrics_identity_or_secret_delivery_resources(self):
        self.assertFalse((ROOT / '04-openbao/resources/metrics.yaml').exists())
        for path in (ROOT / '04-openbao/resources').glob('*.yaml'):
            for resource in yaml.safe_load_all(path.read_text()):
                if isinstance(resource, dict):
                    self.assertNotEqual(resource.get('metadata', {}).get('name'), 'openbao-metrics')
        self.assertNotIn('openbao-metrics', (ROOT / '04-openbao/app.yml').read_text())

    def test_api_and_ha_contract_unchanged(self):
        server = self.values['server']
        self.assertTrue(server['ha']['enabled'])
        self.assertEqual(server['ha']['replicas'], 3)
        self.assertEqual(server['ha']['disruptionBudget'], {'enabled': True, 'maxUnavailable': 1})
        self.assertIn('address = "[::]:8200"', server['ha']['raft']['config'])
        self.assertNotIn('unauthenticated_metrics_access', server['ha']['raft']['config'])


if __name__ == '__main__':
    unittest.main()
