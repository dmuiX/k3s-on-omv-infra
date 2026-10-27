"""PostgreSQL API egress is destination-restricted and owned by Live."""
import hashlib
import json
import os
from pathlib import Path
import unittest

import yaml

INFRA = Path(__file__).resolve().parents[1]
LIVE = Path(os.environ.get('PLATFORM_LIVE_REPO', INFRA.parent / 'k3s-on-omv-live'))


class PostgresAPINetwork(unittest.TestCase):
    def test_only_approved_broad_api_rule_changed_from_published_baseline(self):
        docs = list(yaml.safe_load_all(
            (INFRA / '07-k3s-postgres/resources/network-policy.yaml').read_text()))
        policy, = [d for d in docs if d['metadata']['name'] == 'allow-postgresql']
        # Reconstruct the published snapshot with the one approved removal undone.
        # This protects every other rule, including the separate ingress peers.
        policy['spec']['egress'].append({'ports': [
            {'protocol': 'TCP', 'port': 443}, {'protocol': 'TCP', 'port': 6443}]})
        digest = hashlib.sha256(json.dumps(docs, sort_keys=True).encode()).hexdigest()
        self.assertEqual(digest,
                         '32dbecea5dc2df129cc3c8000da1766bac66ba0727691aa737f55d8fab9530e4')

    def test_infra_has_no_unrestricted_api_egress(self):
        docs = yaml.safe_load_all(
            (INFRA / '07-k3s-postgres/resources/network-policy.yaml').read_text())
        for doc in docs:
            if doc.get('kind') != 'NetworkPolicy':
                continue
            for rule in doc['spec'].get('egress', []):
                self.assertTrue(rule.get('to'), 'Every egress rule needs explicit destinations')
                self.assertFalse(any(p['port'] in (443, 6443)
                                     for p in rule.get('ports', [])))

    def test_live_api_destinations_are_exact_addresses(self):
        policy = yaml.safe_load(
            (LIVE / 'infra/k3s-postgres/api-network-policy.yaml').read_text())
        self.assertEqual(policy['metadata']['namespace'], 'cloudnative-pg')
        self.assertEqual(policy['spec']['podSelector'],
                         {'matchLabels': {'cnpg.io/cluster': 'k3s-postgres'}})
        self.assertEqual(policy['spec']['policyTypes'], ['Egress'])
        rules = policy['spec']['egress']
        self.assertEqual(len(rules), 2)
        for rule, port, addresses in zip(rules, (443, 6443), (
                ['10.43.0.1/32'],
                ['192.168.1.2/32', '192.168.1.5/32', '192.168.1.7/32'])):
            self.assertEqual(rule, {
                'to': [{'ipBlock': {'cidr': address}} for address in addresses],
                'ports': [{'protocol': 'TCP', 'port': port}]})

    def test_live_policy_is_discovered_by_existing_application_source(self):
        app = yaml.safe_load((INFRA / '07-k3s-postgres/app.yml').read_text())
        source, = [s for s in app['spec']['sources']
                   if s.get('path') == 'infra/k3s-postgres']
        self.assertEqual(source['repoURL'], 'https://github.com/dmuiX/k3s-on-omv-live.git')
        self.assertNotIn('ref', source)
        self.assertNotIn('directory', source)
        self.assertFalse((LIVE / source['path'] / 'kustomization.yaml').exists())


if __name__ == '__main__':
    unittest.main()
