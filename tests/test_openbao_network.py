"""Offline native OpenBao NetworkPolicy migration contracts, chart 0.30.4.

Baseline captured from security.yaml before migration, not from chart values.
The approved chart selector adds instance=openbao, NOT component=server.
Only the historical ports-only TCP 6443 rule is removed: the additive Live
server API policy supplies exact Service 443 / endpoint 6443 tuples instead.
The old policy remains in the cluster with prune=false until reviewed retirement;
these tests render locally only and do not deploy or retire anything.
"""
import copy
import hashlib
import unittest

import yaml

import test_native_chart_migrations as native

ROOT = native.INFRA
# Exact original document (mapping/array semantics), including ordered peer lists.
BASELINE = yaml.safe_load('''
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-openbao-raft-and-clients
  namespace: openbao
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: openbao
  policyTypes: [Ingress, Egress]
  ingress:
  - from:
    - podSelector:
        matchLabels: {app.kubernetes.io/name: openbao}
    ports: [{protocol: TCP, port: 8201}]
  - from:
    - podSelector: {}
    ports: [{protocol: TCP, port: 8200}]
  - from:
    - podSelector: {}
    - namespaceSelector:
        matchExpressions:
        - key: kubernetes.io/metadata.name
          operator: In
          values: [cert-manager, crossplane-system, vault-secrets-operator,
                   cloudnative-pg, kube-prometheus-stack, radar, audiobookshelf,
                   longhorn, backup-validation, kube-system, istio-gateway]
    ports: [{protocol: TCP, port: 15008}]
  - from:
    - namespaceSelector:
        matchExpressions:
        - key: kubernetes.io/metadata.name
          operator: In
          values: [cert-manager, crossplane-system, vault-secrets-operator,
                   cloudnative-pg, kube-prometheus-stack, radar, audiobookshelf,
                   longhorn, backup-validation, kube-system, istio-gateway]
    ports: [{protocol: TCP, port: 8200}]
  egress:
  - to:
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: cloudnative-pg}
      podSelector:
        matchLabels: {cnpg.io/cluster: k3s-postgres}
    ports: [{protocol: TCP, port: 5432}, {protocol: TCP, port: 15008}]
  - to:
    - podSelector:
        matchLabels: {app.kubernetes.io/name: openbao}
    ports: [{protocol: TCP, port: 8200}, {protocol: TCP, port: 8201},
            {protocol: TCP, port: 15008}]
  - to:
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: kube-system}
    ports: [{protocol: UDP, port: 53}, {protocol: TCP, port: 53},
            {protocol: TCP, port: 443}]
  - ports: [{protocol: TCP, port: 6443}]
''')
# Explicit approved delta; preserve the historical BASELINE and every other rule.
REMOVED_SERVER_API_RULE = {'ports': [{'protocol': 'TCP', 'port': 6443}]}
CHART_EGRESS = copy.deepcopy(BASELINE['spec']['egress'])
CHART_EGRESS.remove(REMOVED_SERVER_API_RULE)

# The default deny must survive byte-for-byte. Injector/server API tuples and snapshot
# HTTPS moved to additive Live policies; test_remaining_api_network checks their
# exact selectors, union of permissions, and source ownership separately.
UNCHANGED = {
    'default-deny': '6d2250940dd9e9476fae56040e13864114fa38348b816c7bcc460e0a643fc545',
}


def effective_policy_types(spec):
    # Kubernetes defaults to Ingress, adding Egress for a nonempty egress list.
    return spec.get('policyTypes', ['Ingress'] + (['Egress'] if spec.get('egress') else []))


class OpenBaoNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = native.load(ROOT / '04-openbao/app.yml')
        cls.values = cls.app['spec']['sources'][0]['helm']['valuesObject']
        cls.docs = native.render(cls.app)

    def test_captured_baseline_is_exact(self):
        self.assertEqual(native.digest(BASELINE),
                         'b6116d71f04d53ee4ee75183dca3ba455b3736f6fc7da6e10b6dc091bc037489')

    def test_values_preserve_all_rules_except_migrated_broad_server_api(self):
        self.assertEqual(BASELINE['spec']['egress'].count(REMOVED_SERVER_API_RULE), 1)
        policy = self.values['server']['networkPolicy']
        self.assertEqual(policy, {
            'enabled': True,
            'ingress': BASELINE['spec']['ingress'],
            'egress': CHART_EGRESS,
        })

    def check_rendered_policy(self, app, docs):
        source = app['spec']['sources'][0]
        self.assertEqual((source['chart'], source['targetRevision']), ('openbao', '0.30.4'))
        policy, = native.kind(docs, 'NetworkPolicy')
        self.assertEqual(policy['apiVersion'], BASELINE['apiVersion'])
        self.assertEqual(policy['metadata']['name'], 'openbao')
        # Helm uses the release namespace when the template omits metadata.namespace.
        self.assertEqual(policy['metadata'].get('namespace', app['spec']['destination']['namespace']),
                         'openbao')
        selector = {'matchLabels': {
            'app.kubernetes.io/name': 'openbao',
            'app.kubernetes.io/instance': 'openbao',
        }}
        self.assertEqual(policy['spec']['podSelector'], selector)
        self.assertNotIn('component', selector['matchLabels'])
        expected = copy.deepcopy(BASELINE['spec'])
        expected['podSelector'] = selector
        expected['egress'] = CHART_EGRESS
        actual = copy.deepcopy(policy['spec'])
        self.assertNotIn('policyTypes', actual, 'Pinned template relies on Kubernetes defaults')
        actual['policyTypes'] = effective_policy_types(actual)
        self.assertEqual(actual, expected,
                         'Only the ports-only TCP 6443 rule may move to the exact Live API policy')
        statefulset, = native.kind(docs, 'StatefulSet')
        labels = statefulset['spec']['template']['metadata']['labels']
        for key, value in selector['matchLabels'].items():
            self.assertEqual(labels[key], value)
        self.assertEqual(labels['component'], 'server')
        # PDB's server component selector is intentionally different from the policy.
        pdb, = native.kind(docs, 'PodDisruptionBudget')
        self.assertEqual(pdb['spec']['selector']['matchLabels']['component'], 'server')
        self.assertFalse(native.kind(docs, 'HTTPRoute'), 'service-route is the sole route owner')

    def test_exact_cached_chart_render_and_workload_targeting(self):
        self.check_rendered_policy(self.app, self.docs)

    def test_chart_has_no_unrestricted_api_egress(self):
        policy, = native.kind(self.docs, 'NetworkPolicy')
        for spec in (self.values['server']['networkPolicy'], policy['spec']):
            for rule in spec['egress']:
                # Every retained chart rule must constrain its destinations,
                # including the existing kube-system TCP 443 permission.
                self.assertTrue(rule.get('to'), 'Chart must not grant unrestricted API egress')

    def test_live_values_are_only_used_by_route_chart(self):
        route, = [s for s in self.app['spec']['sources']
                  if s.get('path') == 'charts/service-route']
        self.assertEqual(route['helm']['valueFiles'], ['$live/infra/openbao/route-values.yml'])
        # The server network policy has no private Helm overlay; Live plain
        # policies are additive and must not change the native chart contract.
        self.assertNotIn('valueFiles', self.app['spec']['sources'][0]['helm'])

    def test_security_policy_identities_are_stable(self):
        documents = (ROOT / '04-openbao/resources/security.yaml').read_text().split('---\n')
        hashes = {yaml.safe_load(text)['metadata']['name']:
                  hashlib.sha256(text.encode()).hexdigest() for text in documents}
        self.assertEqual(set(hashes), {
            'default-deny', 'allow-openbao-agent-injector', 'allow-openbao-snapshot-egress'})
        self.assertEqual({name: hashes[name] for name in UNCHANGED}, UNCHANGED)
        self.assertFalse(self.app['spec']['syncPolicy']['automated']['prune'],
                         'Renamed old resource must await reviewed retirement')

    def test_policy_type_defaulting(self):
        self.assertEqual(effective_policy_types({}), ['Ingress'])
        self.assertEqual(effective_policy_types({'egress': []}), ['Ingress'])
        self.assertEqual(effective_policy_types({'egress': [{}]}), ['Ingress', 'Egress'])
        self.assertEqual(effective_policy_types(BASELINE['spec']), ['Ingress', 'Egress'])

    def test_route_disabled_and_metrics_deferred_without_changing_ha(self):
        server = self.values['server']
        self.assertIs(server['gateway']['httpRoute']['enabled'], False)
        self.assertEqual(server['ha']['disruptionBudget'], {'enabled': True, 'maxUnavailable': 1})
        self.assertNotIn('telemetry {', server['ha']['raft']['config'])
        self.assertNotIn('serverTelemetry', self.values)
        self.assertNotIn('unauthenticated_metrics_access', server['ha']['raft']['config'])


if __name__ == '__main__':
    unittest.main()
