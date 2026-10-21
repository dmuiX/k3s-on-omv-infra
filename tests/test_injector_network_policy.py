"""The injector needs its own policy; server labels do not select it."""
from pathlib import Path
import unittest

import yaml


class InjectorNetworkPolicyTest(unittest.TestCase):
    def test_injector_api_webhook_and_ambient_probes_are_allowed(self):
        path = Path(__file__).resolve().parents[1] / '04-openbao/resources/security.yaml'
        policies = [doc for doc in yaml.safe_load_all(path.read_text())
                    if doc.get('kind') == 'NetworkPolicy']
        selected = [doc for doc in policies if doc['spec']['podSelector'].get('matchLabels') == {
            'app.kubernetes.io/name': 'openbao-agent-injector'}]
        self.assertEqual(len(selected), 1, 'Injector is isolated by default-deny without its own allow policy')
        spec = selected[0]['spec']
        self.assertEqual(set(spec['policyTypes']), {'Ingress', 'Egress'})
        for port in (443, 6443):
            self.assertTrue(any({'protocol': 'TCP', 'port': port} in rule['ports']
                                and rule.get('to') for rule in spec['egress']))
        dns = [rule for rule in spec['egress']
               if {'protocol': 'UDP', 'port': 53} in rule['ports']]
        self.assertEqual(dns[0]['to'][0]['namespaceSelector']['matchLabels'],
                         {'kubernetes.io/metadata.name': 'kube-system'})
        ingress = spec['ingress']
        self.assertTrue(any({'protocol': 'TCP', 'port': 8080} in rule['ports']
                            for rule in ingress))
        sources = [peer.get('ipBlock', {}).get('cidr')
                   for rule in ingress for peer in rule.get('from', [])]
        for cidr in ('192.168.1.2/32', '192.168.1.5/32', '192.168.1.7/32', '169.254.7.127/32'):
            self.assertIn(cidr, sources)
        self.assertTrue(all(rule.get('from') for rule in ingress))


if __name__ == '__main__':
    unittest.main()
