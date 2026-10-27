# Shared VSO connection, isolated identities

VSO remains pinned to chart/operator **1.6.0**. Its native
`defaultVaultConnection.enabled: true` template creates exactly one
`VaultConnection` named `default` in the Helm release namespace,
`vault-secrets-operator`. Only `address` is configured, pointing to
`http://openbao-active.openbao.svc.cluster.local:8200`; the chart renders its
native `skipTLSVerify: false` default. No custom connection chart/file is needed.

Each consumer explicitly sets
`VaultAuth.spec.vaultConnectionRef: vault-secrets-operator/default`.
This is transport configuration reuse, not authentication reuse.
`defaultAuthMethod.enabled` remains false; no default VaultAuth or
VaultAuthGlobal is introduced.

## Verified upstream mechanism

Inspected the chart downloaded with `helm pull vault-secrets-operator --repo
https://helm.releases.hashicorp.com --version 1.6.0 --untar`:

- `templates/default-vault-connection.yaml` fixes the name to `default` and
  namespace to `.Release.Namespace`; `values.yaml` supplies disabled-by-default
  connection and auth creation independently.
- `crds/secrets.hashicorp.com_vaultauths.yaml` documents the namespace-qualified
  reference. In [v1.6.0 common/common.go](https://github.com/hashicorp/vault-secrets-operator/blob/v1.6.0/common/common.go),
  `GetConnectionNamespacedName` calls `ParseResourceRef`, which splits
  `namespace/name` into a Kubernetes namespaced reference.
- `crds/secrets.hashicorp.com_vaultconnections.yaml` contains address, TLS,
  headers and timeout fields, but **no `allowedNamespaces`** and no login
  identity. `allowedNamespaces` on VaultAuth/VaultAuthGlobal governs auth reuse,
  not connection reuse; it is neither needed nor supported on VaultConnection.
- Rendered the pinned chart using the actual inline Application values and
  checked its connection against the packaged CRD fields and requirements.

## Consumers and unchanged boundaries

Allowed by repository convention: cert-manager DNS01, Longhorn OIDC proxy,
OpenBao snapshots, SMB backup credentials, platform R2 credentials, PostgreSQL
manager credentials, backup validation, Grafana, Alertmanager, Prometheus K8up
credentials, Radar and Audiobookshelf. The connection API does not enforce this
list: permissions to create/change VaultAuth and OpenBao role bindings/policies
remain the controls. Protect modification of the operator-namespace connection.

All ServiceAccounts, Kubernetes roles/mounts/audiences/token lifetimes, OpenBao
roles/policies, secret paths, refresh and restart behavior stay unchanged.
Transformations use the coordinated [required-value helper](vso-required-values.md)
while preserving existing validation and successful output bytes; previously
unvalidated DNS/backup-validation fields gain required-value checks. Existing per-consumer
identities already model separate trust boundaries, even where consumers share a
namespace or endpoint. No additional identity reuse is justified.
`charts/postgres-consumer` consumes an external `vaultAuthRef`; it creates neither
VaultAuth nor VaultConnection and needs no transport change. Historical migration fixtures retain their old resources; tests adapt
only the expected connection ownership and shared-helper syntax differences.

Grafana remains database-only here: its existing `grafana-vso` ServiceAccount
and VaultAuth are preserved, with only the connection reference changed. Grafana
admin provisioning, chart-native ServiceAccount adoption and credential ownership
changes belong to #66 and are not dependencies of this change.

OpenBao metrics is outside this change; no metrics connection/auth resources or
tests are changed. The single-connection assertion concerns the rendered operator
chart, not an uncoordinated whole-cluster claim.

## Rollout and rollback (procedure only; not executed)

1. Sync the VSO Application first. Its parent sync wave is **2**; verify the
   shared connection and required-value helper exist and report current
   Ready/Healthy conditions before moving consumers. Existing local connections can remain during transition.
2. Sync Infra consumers (parent waves 6–9), then coordinated Apps consumers.
   Check VaultAuth and secret-resource status and destination Secret metadata
   only; do not inspect credential values. Confirm existing refresh/restart
   behavior. Remove obsolete local connections only after no references remain.
   Pruning is disabled on these Applications, so deleting source documents does
   not necessarily delete their live objects: explicitly review obsolete
   resources in a separately approved cleanup.
3. The root's custom Application health evaluation gates current child rollouts,
   but waves order children within that root only. They are not cross-repository
   transactions and do not guarantee every custom-resource condition is healthy.
   Audiobookshelf's internal negative waves are local to that Application and
   cannot order it against Infra. Explicitly gate Apps rollout on the shared
   connection; transient missing-connection reconciliation is not a readiness
   guarantee. The endpoint may itself become available later than wave 2.

Rollback: restore/reconcile the old namespaced connections first, then restore
local VaultAuth references; verify reconciliation before disabling the shared
chart connection. Do not disable it while any qualified references remain.
Never alter identity bindings or reissue credentials for this transport-only
rollback.

## Local regression checks

`tests/test_vso_connection.py` checks qualified references, isolated role/SA
pairs, chart ownership, disabled shared auth and packaged CRD compatibility.
Set `PLATFORM_APPS_REPO` to the coordinated Apps checkout and `VSO_CHART_DIR` to
an extracted **1.6.0** chart for offline testing; otherwise the chart render
check skips explicitly. Related PostgreSQL, OIDC and raw Radar inventory
tests preserve their pre-migration identity/secret baselines.
