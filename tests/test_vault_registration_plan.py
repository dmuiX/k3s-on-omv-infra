#!/usr/bin/env python3
import copy
import importlib.util
import pathlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('planner', pathlib.Path(__file__).resolve().parents[1] / 'tools/vault_registration_plan.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def fixture():
    names = sorted(p.IN_USE | p.FUTURE_46 | p.CONDITIONAL_46 | {'unuseds.aws.vault.upbound.io'})
    types, mrds, results = [], [], {}
    for n in names:
        group = n.split('.', 1)[1]
        types.append({'name': n, 'uid': 'crd-' + n, 'group': group, 'plural': n.split('.')[0], 'kind': 'Unused' if n.startswith('unuseds.') else n.split('.')[0].title(), 'scope': 'Cluster', 'owners': [{'kind': 'ProviderRevision', 'uid': 'revision'}, {'kind': 'ManagedResourceDefinition', 'uid': 'mrd-' + n}]})
        mrds.append({'name': n, 'uid': 'mrd-' + n, 'state': 'Active', 'owners': [{'kind': 'ProviderRevision', 'uid': 'revision'}, {'kind': 'Provider', 'uid': 'provider'}]})
        results[n] = {'count': 0, 'objects': [], 'checked': 1000}
    inv = {'types': types, 'results': results, 'cursor': len(types)}
    ctl = {'created': 1000, 'git_revision': 'git-sha', 'providers': [{'metadata': {'name': 'provider-vault', 'uid': 'provider'}, 'spec': {'package': p.PINNED_PACKAGE}}], 'providerrevisions': [{'metadata': {'name': 'revision', 'uid': 'revision', 'ownerReferences': [{'kind': 'Provider', 'uid': 'provider'}]}, 'spec': {'desiredState': 'Active', 'image': p.PINNED_PACKAGE}, 'status': {'capabilities': ['SafeStart'], 'conditions': [{'type': t, 'status': 'True'} for t in ['RuntimeHealthy', 'RevisionHealthy', 'RuntimeActive']]}}], 'mrds': mrds, 'managedresourceactivationpolicies': [{'spec': {'activate': sorted(p.IN_USE)}}], 'compositions': 0, 'compositionrevisions': 0, 'compositeresourcedefinitions': 0}
    return inv, ctl


class PlannerTests(unittest.TestCase):
    def setUp(self): self.inv, self.ctl = fixture()
    def plan(self, **kw): return p.plan(self.inv, self.ctl, now=1001, **kw)
    def test_reservations_and_no_mutation(self):
        out = self.plan()
        self.assertEqual([i['name'] for i in out['candidates']], ['unuseds.aws.vault.upbound.io'])
        self.assertEqual(len(out['retained']), 19)
        self.assertFalse(out['production_mutation_permitted'])
        self.assertEqual(out['plan_sha256'], self.plan()['plan_sha256'])
    def test_nonempty_protected(self):
        self.inv['results']['unuseds.aws.vault.upbound.io'].update(count=1, objects=[{'uid': 'live'}])
        self.assertEqual(self.plan()['candidates'], [])
    def test_failed_unknown_count(self):
        self.inv['results']['unuseds.aws.vault.upbound.io'] = {'error': 'HTTP404', 'checked': 1000}
        with self.assertRaises(ValueError): self.plan()
    def test_count_null_or_forged(self):
        for count in (None, True, -1, 1):
            with self.subTest(count=count):
                inv = copy.deepcopy(self.inv); inv['results']['unuseds.aws.vault.upbound.io']['count'] = count
                with self.assertRaises(ValueError): p.plan(inv, self.ctl, now=1001)
    def test_incomplete(self):
        self.inv['cursor'] -= 1
        with self.assertRaises(ValueError): self.plan()
    def test_stale_or_future(self):
        for stamp in (1, 2000):
            self.inv['results']['unuseds.aws.vault.upbound.io']['checked'] = stamp
            with self.assertRaises(ValueError): p.plan(self.inv, self.ctl, now=4000 if stamp == 1 else 1001)
    def test_owner_mismatch(self):
        for field in ('types',):
            inv = copy.deepcopy(self.inv); next(i for i in inv[field] if i['name'].startswith('unuseds.'))['owners'][1]['uid'] = 'other'
            with self.assertRaises(ValueError): p.plan(inv, self.ctl, now=1001)
    def test_mrd_owner_mismatch(self):
        next(i for i in self.ctl['mrds'] if i['name'].startswith('unuseds.'))['owners'][0]['uid'] = 'other'
        with self.assertRaises(ValueError): self.plan()
    def test_package_or_capability_change(self):
        for key, value in [('package', 'other'), ('capabilities', [])]:
            ctl = copy.deepcopy(self.ctl)
            if key == 'package': ctl['providers'][0]['spec'][key] = value
            else: ctl['providerrevisions'][0]['status'][key] = value
            with self.assertRaises(ValueError): p.plan(self.inv, ctl, now=1001)
    def test_unhealthy(self):
        self.ctl['providerrevisions'][0]['status']['conditions'][0]['status'] = 'False'
        with self.assertRaises(ValueError): self.plan()
    def test_composition_blocks(self):
        self.ctl['compositions'] = 1
        with self.assertRaises(ValueError): self.plan()
    def test_future_reserved_api_missing(self):
        self.ctl['mrds'] = [m for m in self.ctl['mrds'] if m['name'] not in p.CONDITIONAL_46]
        with self.assertRaises(ValueError): self.plan()
    def test_git_ref_and_broad_ref_protected(self):
        self.assertFalse(self.plan(refs=[('aws.vault.upbound.io', 'Unused')])['candidates'])
        self.assertFalse(self.plan(broad_groups=['aws.vault.upbound.io'])['candidates'])
    def test_activation_wildcard_protected(self):
        self.ctl['managedresourceactivationpolicies'][0]['spec']['activate'] = ['*']
        self.assertFalse(self.plan()['candidates'])
    def test_stale_control_blocks(self):
        self.ctl['created'] = 0
        with self.assertRaises(ValueError): p.plan(self.inv, self.ctl, now=4000)
    def test_wildcard_or_mismatched_identity_blocks(self):
        for field, value in [('plural', '*'), ('group', 'other.example.com'), ('scope', 'Unknown')]:
            inv = copy.deepcopy(self.inv); inv['types'][0][field] = value
            with self.assertRaises(ValueError): p.plan(inv, self.ctl, now=1001)
    def test_duplicate_identity_blocks(self):
        self.inv['types'].append(copy.deepcopy(self.inv['types'][0]))
        with self.assertRaises(ValueError): self.plan()
    def test_guard_scoped_create_only(self):
        out = self.plan(); guard = p.creation_guard(out)
        policy, binding = guard['items']
        rules = policy['spec']['matchConstraints']['resourceRules']
        self.assertEqual(rules, [{'apiGroups': ['aws.vault.upbound.io'], 'apiVersions': ['*'], 'operations': ['CREATE'], 'resources': ['unuseds']}])
        self.assertEqual(binding['spec']['validationActions'], ['Deny'])
        self.assertEqual(policy['spec']['failurePolicy'], 'Fail')
        self.assertEqual(policy['metadata']['annotations']['ram-plan-sha256'], out['plan_sha256'])
    def test_header_scan(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d); path = root / '07-apps'; path.mkdir()
            (path / 'x.yaml').write_text('apiVersion: aws.vault.upbound.io/v1alpha1\nkind: Unused\n---\napiVersion: kv.vault.upbound.io/v1alpha1\n')
            (path / 'application-health-config.yml').write_text('apiVersion: ignored.vault.upbound.io/v1\nkind: Foo\n')
            refs, broad = p.git_refs(root)
            self.assertIn(('aws.vault.upbound.io', 'Unused'), refs)
            self.assertIn('kv.vault.upbound.io', broad)
            self.assertNotIn(('ignored.vault.upbound.io', 'Foo'), refs)


if __name__ == '__main__': unittest.main()
