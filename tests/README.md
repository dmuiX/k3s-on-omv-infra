# Local checks

Run from the infra repository:

```sh
ruby tests/verify-bootstrap.rb   # multi-source Helm, waves, values, storage constraints
ruby tests/verify-cert-manager-network-policy.rb # wave-2 ownership and exact private API egress
ruby tests/verify-application-health.rb # minimal child Application health passthrough (Lua or pinned npx fallback)
ruby tests/verify-openbao-pki.rb # native PKI identities/RBAC/issuers/certificate render
ruby tests/verify-postgresql.rb # Crunchy PGO HA/storage/backup/policy and pinned chart renders
ruby tests/verify-ingress.rb     # no live-specific resources selected by the public root
ruby tests/verify-longhorn-encryption.rb # Helm override and complete key references
ruby tests/verify-pi-placement.rb # explicit restricted-Pi tolerations; monitoring exclusion
ruby tests/verify-charts.rb      # pinned Helm renders + safe local templates (network)
```

The ordinary Ruby checks need this repository; the OpenBao PKI check also calls
`kubectl kustomize` locally. The Application-health test uses a
local Lua interpreter when available and otherwise executes the pinned
`fengari-node-cli@0.1.0` fallback through `npx`; the first fallback run may need
registry access. `LUA=/path/to/lua` overrides automatic selection. They need no
kubeconfig or cluster.
The chart test additionally needs Helm and network access. It renders each pinned
upstream chart with the Git values in an isolated temporary Helm cache, applies
the same Longhorn ConfigMap override, node annotations, and monitoring
StorageClass as Argo, and validates the desired state.
Chart diagnostics and Secret values are never printed.
Restore tests verify decimal ordinal-to-resource/PVC/path mapping from CLI, JSON
and YAML values (including the upper bound) and reject malformed, fractional,
negative, boolean, null, quoted-string and out-of-range ordinals instead of silently
restoring to ordinal zero. Rendering does not prove installation, ingress TLS,
storage health or backup recovery.

The first-phase render checks wave-1 monitoring CRDs without workloads, the
post-Longhorn monitoring claim/templates, metadata-only OMV/Wyse node
annotations (without polling resources or Longhorn Node CRs), OpenBao at three
Raft server pods with two PVC templates each, and three replicas for new Longhorn
volumes. They do not change existing volume replica counts or prove placement
and recovery.

The public root owns the complete steady-state child Application inventory,
including OpenBao PKI and PostgreSQL, without an exclusion list. Tests assert
that no custom in-cluster OpenBao polling reconciler remains and that the PKI
Application renders only cert-manager identities/RBAC, ClusterIssuers, and the
internal Certificate. OpenBao API configuration and ceremony belong to Ansible.
Controller charts and public values remain here; route, certificate, backup,
and private Kubernetes API endpoint values remain in the private values
repository.
With sibling Live and Bootstrap checkouts, run
`ruby tests/verify-private-values.rb`. It uses the current Bootstrap Traefik
HelmChartConfig template by default; an alternate rendered config or template
may be passed as the second argument. It renders the local chart with the real
values without printing private identifiers or Secret data.

Before any live sync, separately review: initial Argo CD read-only Git access
(the private repo cannot bootstrap its own credential through OpenBao), Argo CD
child-Application health customization, host storage prerequisites, disposable
Longhorn PVC provisioning/write/read/reattach, the monitoring CRD ownership
handoff and new PVCs (see `03-core/kube-prometheus-stack/README.md`), OpenBao unseal and recovery,
Certificate Ready, Gateway/route status, independent backup/restore and the
Argo automated prune/self-heal settings. Cluster-mutating tests need separate
approval and a cleanup plan.
