# Service ownership and GitOps credentials

## Ownership

OpenBao is the secret store, not the owner of every consumer's integration.

| Application | Repository directory | Responsibility |
| --- | --- | --- |
| `cloudnative-pg` | Infra `02-controllers/cloudnative-pg` | PostgreSQL operator, CRDs and webhook |
| `k3s-postgres` | Infra `07-k3s-postgres` | Concrete `k3s-postgres` Cluster, manager login/password, connection, cluster backup and retention |
| `longhorn` | Infra `02-controllers/longhorn` | Storage controller and early storage prerequisites |
| `longhorn-config` | Infra `06-platform/longhorn-config` | Longhorn's OIDC identities, proxy, credentials and route |
| `radar` | Apps `radar` | Radar UI, OpenBao identity/client/provider, credential issuance, application database/role, RBAC and Secret consumers |
| `argocd-config` | Infra `01-bootstrap/argocd-config` | Local Argo account definition and Argo application/project read permissions |
| `platform-credentials` | Infra `06-platform/backup-credentials` | Shared platform R2/SMB access and Secret delivery |
| `openbao-config` | Infra `06-platform/openbao-config` | General OpenBao mounts, userpass backend and the shared standard OIDC `profile` scope |

Other consumer-specific OpenBao policies/auth roles live in their owner's `resources/openbao-access.yaml`: OpenBao snapshots, certificate DNS validation, monitoring and backup validation. OpenBao's own PeerAuthentication belongs to the core OpenBao Application. The general provider-vault `Mount` and `Backend` CRDs have Kubernetes scope `Cluster`; their manifests intentionally have no `metadata.namespace`, regardless of the Application's default destination namespace.

Live keeps Longhorn identity/route settings under `infra/longhorn`, Radar's non-secret issuer/callback/route values in `apps/radar/values.yml`, and database retention settings under `infra/k3s-postgres`. Service DNS names, database resources, Kubernetes namespaces, KV paths, Secret names and PVC identities are unchanged. In particular, the manager secret remains at `kv/postgresql/openbao-manager`; renaming an Application is not a secret/data migration.

Audiobookshelf's local chart owns its OIDC key, client, provider, administrator assignment and app-specific role scope. Its PostSync API configurator consumes the existing shared custody helper, without extra Kubernetes RBAC or OIDC provisioning in Ansible. Since ABS requests the standard `profile` scope, the platform owns that global OpenBao scope exactly once as `OidcScope/oidc-profile`; applications reference it instead of declaring conflicting external `profile` objects. Its template supplies only entity name/preferred_username, not administrator permissions or invented email claims. The provider trust-root ACL adds only the exact Audiobookshelf policy/role/OIDC paths and the shared profile path. The reviewed live trust-policy update and state migration remain separate approvals; the new Application's auto-sync stays disabled.

## Bootstrap ordering

`longhorn` must provide storage before OpenBao starts. Its later `longhorn-config` Application therefore runs in Infra wave 6, after OpenBao/VSO/provider trust, not in controller wave 2.

After Crossplane provider trust is established, Bootstrap activates the Apps root so Radar's administrator group and generated OIDC client exist before attended group assignment. The shared read-only Git credential has already registered the Apps repository at that point. Complete Infra acceptance and final Radar health remain later gates. Radar's local OIDC Helm chart renders the Apps-owned identity resources using the issuer and callback URLs from Live; plain YAML is not used for cross-repository value interpolation. The Radar namespace is created before namespace-local credential helpers/jobs. Group membership remains externally managed by the attended ceremony.

`k3s-postgres` remains in wave 7. Its narrowly scoped manager-credential producer runs before the VSO Secret consumers and Cluster. Backup/restore validation follows in wave 8 (`08-backup-validation`); observability follows in wave 9 (`09-kube-prometheus-stack`). The early Prometheus CRD installation remains in wave 1 so validation resources do not depend on the later monitoring stack. Full Infra acceptance still checks all consumer Applications through wave 9 after the attended external credentials have been supplied.

## Native mechanisms and narrowly scoped gaps

provider-vault 4.0.5 creates OIDC keys, scopes, assignments, clients and providers. Longhorn's assignment permits only `radar:admin`; each provider explicitly allows only its generated client ID. Dedicated signing keys allow `*` because the installed key schema has no client-ID reference field and the client needs its key to exist first. Radar requests `openid radar`, including the custom username/groups claims.

Radar's OIDC resources run in wave -20, before its database role (-10) and Database (-5), so the attended group assignment cannot wait on the later database/platform credential ceremony. Credential helper, policy, ServiceAccount and namespaced Argo RBAC run in wave -2; the issuance hook runs in -1 before the UI/VSO consumers in wave 0. This avoids waiting for a Secret consumer before its producer runs.

The pinned provider's [connection mapping](https://github.com/upbound/provider-vault/blob/v4.0.5/apis/namespaced/identity/v1alpha1/zz_oidcclient_terraformed.go) exports only `client_secret`; `client_id` is a status field, not a connection-Secret key. VSO therefore reads `identity/oidc/client/radar` with GET into the separate `radar-oidc` application Secret. This uses native [VaultDynamicSecret requestHTTPMethod/refreshAfter](https://github.com/hashicorp/vault-secrets-operator/blob/v1.6.0/api/v1beta1/vaultdynamicsecret_types.go), which explicitly supports non-leased responses. It is not a KV copy or another issuance operation. The VSO policy permits only read of this one client. No `allowStaticCreds` is set because OIDC has no database-style rotation metadata. No automatic rollout targets are set: non-leased VDS syncs report a write on every refresh; after an explicit client rotation, perform a normal Radar rollout to reload environment values. The existing provider connection Secret is retained for Crossplane state continuity; Radar consumes only the VSO-owned Secret.

CloudNativePG's managed roles own the non-superuser `openbao_manager` login and consume the VSO-delivered basic-auth Secret. `CREATEROLE` permits creation/rotation of application roles it administers; the manager cannot create databases, replicate or bypass row security. Application databases/roles remain with Radar and Grafana, not with the operator installation.

VSO delivers secrets but does not generate random values or Argo API tokens. Radar chart 1.17.0 and oauth2-proxy accept existing Secrets; Radar's empty-secret fallback is process-local, not durable custody. Three consumer-owned Sync-hook Jobs fill only these gaps:

| Job / namespace | Allowed OpenBao KV data |
| --- | --- |
| `longhorn-credentials` / `longhorn` | `longhorn/oidc-proxy`: native client credentials plus random 32-byte cookie key |
| `k3s-postgres-credentials` / `cloudnative-pg` | `postgresql/openbao-manager`: fixed username plus random password |
| `radar-credentials` / `radar` | `radar/auth`: session key; `radar/argocd`: issued account token |

Each Job has a separate ServiceAccount, audience-bound login role, create-only policy and NetworkPolicy. Longhorn/database producers have no Argo permissions or Argo CLI. The central multi-service Job has been removed. A small `adapters/credential-custody` ConfigMap provides identical HTTP/CAS/ownership handling in each consumer namespace; it contains no consumer paths, identity decisions, Argo implementation or combined issuer permissions.

## Argo account versus Radar integration

Argo declares an enabled, apiKey-only `radar` account through `argocd-cm` and `argocd-rbac-cm` in `argocd`. Token registration is in `argocd-secret`, also in `argocd`. These are Argo's native configuration objects, not a Kubernetes User resource.

Radar owns its credential-issuance Job and the RoleBinding that lets that Job use Argo's supported `argocd --core account generate-token`. It does not sign JWTs itself or copy the admin password. Core mode needs namespace informers, so this privileged issuer can read/watch Argo Secrets/configuration but update only `argocd-secret`; it has no cluster-wide RBAC. After issuance it checks acceptance by the real Argo server with certificate-verified TLS. The Radar application receives only the resulting account token through OpenBao/VSO.

## Safety and validation

CAS-0 writes, create-without-update ACLs, read-back verification and ownership checks preserve existing values. Deleted entries with retained history and foreign/malformed entries fail closed. The existing `managed-by: service-credentials-v1` marker and stable Argo token ID are deliberately retained for compatibility with values created before the ownership split. No root token or human password enters these Jobs; CLI responses are withheld and scratch data is memory-backed. Retries reuse custody or revoke only their own uncustodied orphan Argo token. Existing custody with a missing/revoked token fails rather than silently rotating it.

The PostgreSQL plugin splits SQL strings on semicolons unless they encode a JSON array; the existing JSON-wrapped role rotation blocks are retained. Privileged roles are rejected. Targeted Ambient/HBONE rules and the CNPG HTTPS-webhook port exception are unchanged.

Tests cover consumer ownership, namespace/sync-wave ordering, stable Cluster identity, create-only ACLs, cross-consumer denial, first/resumed custody, foreign/tombstoned entries, and real OpenBao OIDC code/PKCE exchanges with provider/group rejection. Full offline validation uses `OPENBAO_TEST_BINARY=/path/to/verified/bao tests/run-offline-tests.sh`. Offline passes do not prove live convergence or human/external-credential acceptance.

Native evidence: [Radar values](https://github.com/skyhook-io/radar/blob/v1.17.0/deploy/helm/radar/values.yaml), [Argo account API](https://github.com/argoproj/argo-cd/blob/v3.5.5/server/account/account.go), [core mode](https://github.com/argoproj/argo-cd/blob/v3.5.5/cmd/argocd/commands/headless/headless.go), [CNPG managed roles](https://cloudnative-pg.io/docs/1.28/declarative_role_management/), installed provider-vault CRDs, and [OpenBao OIDC](https://github.com/openbao/openbao/blob/v2.7.1/internal/vault/identity/store_oidc_provider.go).

## Existing-cluster cutover is a separate operation

Do not treat this ownership change as an unattended rename. Legacy `postgresql`, `tls-certificates` and `letsencrypt-dns01` Applications and the central issuer may still exist; automated pruning is disabled, so Git deletion does not remove them. Publishing the new paths without updating existing child sources can produce comparison/sync errors even while the parent is paused.

1. Quiesce the Infra root and affected old Application synchronizations; retain their configuration for rollback. Stop the legacy `openbao/service-credentials` Job before enabling replacement issuers. Do not run old/new credential issuers concurrently.
2. Publish the public Infra Application base, then Live and Bootstrap. Live `infra/applications/kustomization.yaml` follows the public base on protected `main`; the root's source is the private Live overlay. A public-only change may require a hard refresh of the Infra root because Argo caches remote bases independently of Live. Bootstrap validates ownership/source paths, applies the new root, and resumes a previously paused owned root. Update the existing `openbao-config` specification so it no longer manages consumer resources.
3. Transfer unchanged resources to the new consumer Applications, confirming each tracking owner and source revision. Transfer the former `radar-config` resources to the Apps-owned `radar` Application without changing their external OpenBao identities. Create `k3s-postgres` with its original Cluster/Secret/backup identities. Disable the old `postgresql` and `radar-config` reconcilers during adoption.
4. Only after verified adoption, remove the old `radar-config`, `postgresql`, `tls-certificates` and `letsencrypt-dns01` Applications **non-cascading**, for example `argocd app delete radar-config --cascade=false`. Never delete the Cluster, namespace, PVCs, database roles or KV entries as part of the rename. Never force-remove finalizers to get a green status.
5. Retire the legacy issuer's ServiceAccount, RoleBinding, Job and its narrowly named OpenBao policy/login role using an authorized administrator; account for any still-valid short-lived issuer tokens. Verify old ownership is gone before resuming automated reconciliation and final acceptance.

For Radar, also remove the former Bootstrap-owned `auth.oidc.clientID` Helm parameter and its root `ignoreDifferences` entry during the controlled cutover. The new values use only the VSO Secret reference and argument expansion; no generated client ID should remain in the Application specification. Confirm that the `radar-oidc` Secret is VSO-owned and that the provider's retained `radar-oidc-client` connection Secret has not been adopted by VSO. These two controllers must never write the same Secret.

This source reorganization itself does not perform that live cutover or update a live root policy.

### Preserve external auth objects during ownership transfer

Consumer `Policy` and `AuthBackendRole` resources use native Crossplane
`deletionPolicy: Orphan`, consistent with the provider's deliberately absent
OpenBao delete permission. The installed CRD exposes this supported lifecycle
field. An owner/path migration must not destroy working login roles or ACLs.

If a previous prune already marked an owned managed resource for deletion,
verify its UID, external identity and provider reference, then change only its
`spec.deletionPolicy` to `Orphan`. Let Crossplane complete its normal finalization;
never patch away finalizers. Recreate/adopt the same external object through the
consumer's Git manifest and require fresh Ready/Synced status. Preserve the
existing policy body, service-account bindings, namespace, audience and KV data.
Legacy issuer permissions remain a separately reviewed administrator cleanup,
not a reason to grant broad delete access to the provider.

Critical Mount/Backend resources additionally declare `crossplane.io/external-name`
with the verified existing backend path (`kv`, `database`, `userpass`). Orphaning
preserves the external engine; that external-name annotation tells the new
management object to import it rather than attempting an enable/create request.
Do not delete an existing engine to resolve a `path is already in use` response.

### Runtime consumer checks

OAuth2 Proxy 7.15.5 decodes cookie keys using URL-safe Base64
([native decoder](https://github.com/oauth2-proxy/oauth2-proxy/blob/v7.15.5/pkg/encryption/utils.go)).
The retained KV contract uses standard Base64. VSO changes only the alphabet in
the delivered value, preserving all 32 key bytes and never rotating the KV entry.
A native Go/Sprig template render tests a fixture containing both `+` and `/`.

Radar's pinned native Argo CLI is 250176085 bytes. Its non-secret `/tools` copy
uses disk-backed ephemeral storage; a memory-backed volume charged that copy to
the 128Mi init-container cgroup and caused OOM kills. Temporary credentials under
`/work` remain memory-backed. Normal Argo hook replacement retries failed
short-lived Jobs; it must not delete PVCs, databases, backends or credential data.

A representation-only Helm source change can leave a child Synced/Healthy while
its last successful operation still records the old values-file source. Request
a normal full native Argo sync of that child to record adoption; do not loosen
its current-source/operation acceptance checks or manually change health status.
