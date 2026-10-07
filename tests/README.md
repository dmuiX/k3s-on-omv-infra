# Offline checks

Run from the infra repository:

```sh
ruby tests/verify-bootstrap.rb   # multi-source Helm, waves, values, storage constraints
ruby tests/verify-application-health.rb # child sync/health gating (requires Lua)
ruby tests/verify-openbao-access.rb # dedicated SA, Git-managed ACL, job/loop render
ruby tests/verify-openbao-pki.rb # mandatory staged gate, PKI identities/RBAC/issuers render
ruby tests/verify-postgresql.rb # Crunchy PGO HA/storage/backup/policy and pinned chart renders
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_openbao_*.py'
ruby tests/verify-ingress.rb     # no live-specific resources selected by the public root
ruby tests/verify-longhorn-encryption.rb # Helm override and complete key references
ruby tests/verify-pi-placement.rb # explicit restricted-Pi tolerations; monitoring exclusion
ruby tests/verify-charts.rb      # pinned Helm renders + safe local templates (network)
```

The ordinary Ruby/Python checks need this repository; the OpenBao equivalence
check also calls `kubectl kustomize` locally, and the Application-health test
requires a Lua interpreter (or `LUA=/path/to/lua`) so syntax/fixtures cannot be
silently skipped. They need no kubeconfig or cluster.
The chart test additionally needs Helm and network access. It renders each pinned
upstream chart with the Git values in an isolated temporary Helm cache, applies
the same final Longhorn ConfigMap override as Argo, and validates the result.
Chart diagnostics and Secret values are never printed.
Restore tests verify decimal ordinal-to-resource/PVC/path mapping from CLI, JSON
and YAML values (including the upper bound) and reject malformed, fractional,
negative, boolean, null, quoted-string and out-of-range ordinals instead of silently
restoring to ordinal zero. Rendering does not prove installation, ingress TLS,
storage health or backup recovery.

The first-phase render checks wave-1 monitoring CRDs without workloads, the
post-Longhorn monitoring claim/templates, OpenBao at three Raft server pods with
two PVC templates each, and three replicas for new Longhorn volumes. They do not
change existing volume replica counts or prove placement and recovery.

The same public root owns all regular child Applications. Mandatory OpenBao PKI
is explicitly selected by the root include but safely excluded by default until
the external-root ceremony and guarded installation pass. Its Application is
already pinned; the GitOps bootstrap then promotes the root configuration to
activate it rather than registering it independently. PostgreSQL is implemented
as the same guarded Wave-6 phase and becomes mandatory with PKI activation. Tests
verify both the inactive default inventory and the activated Wave-5/6 cohorts.
Controller charts and public values remain here; route, certificate and backup
identifiers remain in the private values repository.
With that checkout, run `ruby tests/verify-private-values.rb`
(also requires Helm and the host Traefik configuration). It renders the local
chart with the real values without printing private identifiers or Secret data.

Before any live sync, separately review: initial Argo CD read-only Git access
(the private repo cannot bootstrap its own credential through OpenBao), Argo CD
child-Application health customization, host storage prerequisites, disposable
Longhorn PVC provisioning/write/read/reattach, the monitoring CRD ownership
handoff and new PVCs (see `03-core/kube-prometheus-stack/README.md`), OpenBao unseal and recovery,
Certificate Ready, Gateway/route status, independent backup/restore and the
Argo automated prune/self-heal settings. Cluster-mutating tests need separate
approval and a cleanup plan.
