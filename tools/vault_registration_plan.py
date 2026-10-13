#!/usr/bin/env python3
"""Offline, fail-closed Vault registration planner. No mutation/client mode exists."""
import argparse
import fnmatch
import hashlib
import json
import pathlib
import re
import time

IN_USE = frozenset('''secretbackendconnections.database.vault.upbound.io
secretbackendstaticroles.database.vault.upbound.io
oidcclients.identity.vault.upbound.io
oidckeys.identity.vault.upbound.io
oidcproviders.identity.vault.upbound.io
oidcscopes.identity.vault.upbound.io'''.split())
FUTURE_46 = frozenset('''mounts.vault.vault.upbound.io
policies.vault.vault.upbound.io
secretbackendv2s.kv.vault.upbound.io
backends.auth.vault.upbound.io
policies.password.vault.upbound.io
authbackendroles.kubernetes.vault.upbound.io
secretbackendroles.database.vault.upbound.io
groups.identity.vault.upbound.io
oidcroles.identity.vault.upbound.io
secretbackendroles.pki.vault.upbound.io
secretbackendconfigurls.pki.vault.upbound.io
secretbackendcrlconfigs.pki.vault.upbound.io'''.split())
# Explicit goal includes assignments; non-importable existing assignments are
# NOT approved for adoption. Reserve the API nevertheless, rather than race #46.
CONDITIONAL_46 = frozenset(['oidcassignments.identity.vault.upbound.io'])
PINNED_PACKAGE = 'xpkg.upbound.io/upbound/provider-vault:v4.0.4'


def require(value, message):
    if not value:
        raise ValueError(message)


def unique(items, name_key='name'):
    result = {i[name_key]: i for i in items}
    require(len(result) == len(items), 'Duplicate identities in snapshot')
    return result


def owned(item, kind, uid):
    return any(o.get('kind') == kind and o.get('uid') == uid for o in item.get('owners', []))


def git_refs(root):
    """Read YAML header lines only. Treat an ambiguous referenced group broadly."""
    refs = set()
    broad_groups = set()
    for parent in root.iterdir():
        if not parent.is_dir() or not re.match(r'^\d\d-', parent.name):
            continue
        for path in sorted(parent.rglob('*')):
            if path.suffix not in ('.yaml', '.yml') or path.is_symlink():
                continue
            # Exclusions/health snippets are not desired managed objects.
            if path.name == 'application-health-config.yml':
                continue
            lines = path.read_text().splitlines()
            for block in re.split(r'^\s*---\s*$', '\n'.join(lines), flags=re.M):
                groups = re.findall(r'^\s*apiVersion:\s*[\"\']?([a-z0-9.]+vault(?:\.m)?\.upbound\.io)/', block, re.M)
                kinds = re.findall(r'^\s*kind:\s*[\"\']?([A-Za-z][A-Za-z0-9]+)', block, re.M)
                if groups and kinds:
                    refs.update((g, k) for g in groups for k in kinds)
                elif groups:
                    broad_groups.update(groups)
    return refs, broad_groups


def plan(inventory, control, refs=(), broad_groups=(), now=None, max_age=3600):
    now = time.time() if now is None else now
    require(control.get('git_revision'), 'Missing Git baseline')
    require(0 <= now - control.get('created', 0) <= max_age, 'Stale/future control snapshot')
    for resource in ('compositions', 'compositionrevisions', 'compositeresourcedefinitions'):
        require(control.get(resource) == 0, 'Live composition dependency requires separate reviewed mapping: ' + resource)
    providers = [p for p in control['providers'] if p['metadata']['name'] == 'provider-vault']
    require(len(providers) == 1, 'Expected exactly one Vault Provider')
    provider = providers[0]
    require(provider['spec']['package'] == PINNED_PACKAGE, 'Provider package changed')
    revisions = [r for r in control['providerrevisions'] if r['spec'].get('desiredState') == 'Active']
    require(len(revisions) == 1, 'Expected one Active ProviderRevision')
    rev = revisions[0]
    require(rev['spec'].get('image') == PINNED_PACKAGE, 'Revision image changed')
    require('SafeStart' in rev.get('status', {}).get('capabilities', []), 'SafeStart not advertised')
    conditions = {c['type']: c['status'] for c in rev.get('status', {}).get('conditions', [])}
    require(all(conditions.get(t) == 'True' for t in ('RuntimeHealthy', 'RevisionHealthy', 'RuntimeActive')), 'Revision is not healthy/active')
    provider_uid, revision_uid = provider['metadata']['uid'], rev['metadata']['uid']
    require(any(o.get('kind') == 'Provider' and o.get('uid') == provider_uid for o in rev['metadata'].get('ownerReferences', [])), 'Revision Provider ownership mismatch')
    types, mrds = unique(inventory['types']), unique(control['mrds'])
    for name, t in types.items():
        require(re.fullmatch(r'[a-z][a-z0-9]*', t['plural']) and re.fullmatch(r'[a-z0-9]+(?:\.[a-z0-9]+)+', t['group']), 'Wildcard/invalid API identity: ' + name)
        require(name == t['plural'] + '.' + t['group'], 'API name/group mismatch: ' + name)
        require(t['scope'] in ('Cluster', 'Namespaced'), 'Unknown scope: ' + name)
    require(inventory['cursor'] == len(types) and set(inventory['results']) == set(types), 'Incomplete metadata inventory')
    results = inventory['results']
    for name, r in results.items():
        require('error' not in r, 'Metadata inventory error: ' + name)
        require(0 <= now - r['checked'] <= max_age, 'Stale/future inventory: ' + name)
        require(isinstance(r.get('count'), int) and not isinstance(r['count'], bool) and r['count'] == len(r.get('objects', [])), 'Unproven instance count: ' + name)
    activations = [a for p in control['managedresourceactivationpolicies'] for a in p['spec']['activate']]
    refs = set(tuple(r) for r in refs)
    keep, candidates = [], []
    for name, m in sorted(mrds.items()):
        require(name in types, 'MRD without inventoried CRD: ' + name)
        t, r = types[name], results[name]
        reasons = []
        if name in IN_USE: reasons.append('currently-required')
        if name in FUTURE_46: reasons.append('bootstrap-46-reserved')
        if name in CONDITIONAL_46: reasons.append('bootstrap-46-conditional-reserved')
        if r['count']: reasons.append('nonempty')
        if (t['group'], t['kind']) in refs or t['group'] in broad_groups: reasons.append('git-reference')
        if any(fnmatch.fnmatchcase(name, a) for a in activations): reasons.append('live-activation')
        if reasons:
            keep.append({'name': name, 'reasons': sorted(reasons), 'count': r['count']})
            continue
        require(m['state'] == 'Active', 'Unexpected candidate MRD state: ' + name)
        require(owned(m, 'ProviderRevision', revision_uid) and owned(m, 'Provider', provider_uid), 'MRD ownership changed: ' + name)
        require(owned(t, 'ProviderRevision', revision_uid) and owned(t, 'ManagedResourceDefinition', m['uid']), 'CRD ownership changed: ' + name)
        require(t.get('uid') and m.get('uid'), 'Missing UID: ' + name)
        candidates.append({'name': name, 'mrd_uid': m['uid'], 'crd_uid': t['uid'], 'group': t['group'], 'plural': t['plural'], 'scope': t['scope'], 'checked': r['checked']})
    require(IN_USE | FUTURE_46 | CONDITIONAL_46 <= set(mrds), 'Reserved API disappeared; remap required')
    out = {'schema_version': 1, 'mode': 'PLAN_ONLY', 'production_mutation_permitted': False,
           'git_revision': control['git_revision'], 'package': PINNED_PACKAGE,
           'provider_uid': provider_uid, 'revision_uid': revision_uid,
           'retained': keep, 'candidates': candidates,
           'required_gates': ['explicit exact-set approval', 'refresh inventory and ownership immediately before use',
               'coordinate exclusive writers with bootstrap #46', 'load and verify CREATE deny guard first',
               'verify zero instances after guard; UID/resourceVersion preconditions',
               'six-kind Git activation policy deployed and required APIs Established',
               'rollback source and stage checkpoint', 'exactly one provider rollout, no K3s restart',
               'preserved UID and fresh functional database/OIDC acceptance', 'remove owned guard, release lease, warm leader-equivalent RAM/watch comparison']}
    out['plan_sha256'] = hashlib.sha256(json.dumps(out, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return out


def creation_guard(p):
    require(p['mode'] == 'PLAN_ONLY' and p['candidates'], 'No candidates to guard')
    groups = {}
    for t in p['candidates']:
        groups.setdefault(t['group'], []).append(t['plural'])
    rules = [{'apiGroups': [g], 'apiVersions': ['*'], 'operations': ['CREATE'], 'resources': sorted(set(v))} for g, v in sorted(groups.items())]
    name = 'vault-api-withdrawal-ticket35'
    policy = {'apiVersion': 'admissionregistration.k8s.io/v1', 'kind': 'ValidatingAdmissionPolicy',
              'metadata': {'name': name, 'annotations': {'ram-plan-sha256': p['plan_sha256']}},
              'spec': {'failurePolicy': 'Fail', 'matchConstraints': {'resourceRules': rules},
                       'validations': [{'expression': 'false', 'message': 'API type temporarily withdrawn for reviewed registration maintenance.'}]}}
    binding = {'apiVersion': 'admissionregistration.k8s.io/v1', 'kind': 'ValidatingAdmissionPolicyBinding',
               'metadata': {'name': name, 'annotations': {'ram-plan-sha256': p['plan_sha256']}},
               'spec': {'policyName': name, 'validationActions': ['Deny']}}
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [policy, binding]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--inventory', type=pathlib.Path, required=True)
    ap.add_argument('--control', type=pathlib.Path, required=True)
    ap.add_argument('--repo', type=pathlib.Path, required=True)
    ap.add_argument('--output', type=pathlib.Path, required=True)
    args = ap.parse_args()
    require(not args.output.exists() and not args.output.is_symlink(), 'Refusing output overwrite')
    refs, broad = git_refs(args.repo)
    p = plan(json.loads(args.inventory.read_text()), json.loads(args.control.read_text()), refs, broad)
    # Atomic exclusive creation also refuses a raced symlink/existing target.
    import os
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(p, indent=2) + '\n')
    print('PLAN_ONLY:', len(p['candidates']), 'candidates;', len(p['retained']), 'retained; SHA256', p['plan_sha256'])


if __name__ == '__main__':
    main()
