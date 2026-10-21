# Service ownership and GitOps credentials

## Ownership

OpenBao is the secret store, not the owner of every consumer's integration.

| Application | Repository directory | Responsibility |
| --- | --- | --- |
| `cloudnative-pg` | Infra `02-controllers/cloudnative-pg` | PostgreSQL operator, CRDs and webhook |
| `k3s-postgres` | Infra `07-k3s-postgres` | Concrete `k3s-postgres` Cluster, manager login/password, connection, cluster backup and retention |
| `longhorn` | Infra `02-controllers/longhorn` | Storage controller and early storage prerequisites |
| `longhorn-config` | Infra `06-platform/longhorn-config` | Longhorn's OIDC identities, proxy, credentials and route |
| `radar-config` | Infra `06-platform/radar-config` | Radar's namespace, bootstrap identity, OIDC client/provider and credential issuance |
| `radar` | Apps `radar` | Radar UI, application database/role, RBAC and Secret consumers |
| `argocd-config` | Infra `01-bootstrap/argocd-config` | Local Argo account definition and Argo application/project read permissions |
| `platform-credentials` | Infra `06-platform/backup-credentials` | Shared platform R2/SMB access and Secret delivery |
| `openbao-config` | Infra `06-platform/openbao-config` | General OpenBao mounts and userpass backend only |

Other consumer-specific OpenBao policies/auth roles live in their owner's `resources/openbao-access.yaml`: OpenBao snapshots, certificate DNS validation, monitoring and backup validation. OpenBao's own PeerAuthentication belongs to the core OpenBao Application. The general provider-vault `Mount` and `Backend` CRDs have Kubernetes scope `Cluster`; their manifests intentionally have no `metadata.namespace`, regardless of the Application's default destination namespace.

Live keeps Longhorn identity/route settings under `infra/longhorn`, Radar identity settings under `apps/radar`, and database retention settings under `infra/k3s-postgres`. Service DNS names, database resources, Kubernetes namespaces, KV paths, Secret names and PVC identities are unchanged. In particular, the manager secret remains at `kv/postgresql/openbao-manager`; renaming an Application is not a secret/data migration.

## Bootstrap ordering

`longhorn` must provide storage before OpenBao starts. Its later `longhorn-config` Application therefore runs in Infra wave 6, after OpenBao/VSO/provider trust, not in controller wave 2.

`radar-config` is also selected by Infra in wave 6. Its administrator group must exist before attended group assignment and MFA; its generated OIDC client ID must exist before Apps registration. Keeping these prerequisites in a Radar-owned Infra directory avoids requiring access to the private Apps repository before the attended Apps credentials are supplied. Radar's UI and application-specific PostgreSQL resources remain in Apps. The Radar namespace is created before namespace-local credential helpers/jobs. Group membership remains externally managed by the attended ceremony.

`k3s-postgres` remains in wave 7. Its narrowly scoped manager-credential producer runs before the VSO Secret consumers and Cluster. Backup/restore validation follows in wave 8 (`08-backup-validation`); observability follows in wave 9 (`09-kube-prometheus-stack`). The early Prometheus CRD installation remains in wave 1 so validation resources do not depend on the later monitoring stack. Full Infra acceptance still checks all consumer Applications through wave 9 after the attended external credentials have been supplied.

## Native mechanisms and narrowly scoped gaps

provider-vault 4.0.5 creates OIDC keys, scopes, assignments, clients and providers. Longhorn's assignment permits only `radar:admin`; each provider explicitly allows only its generated client ID. Dedicated signing keys allow `*` because the installed key schema has no client-ID reference field and the client needs its key to exist first. Radar requests `openid radar`, including the custom username/groups claims.

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
3. Transfer unchanged resources to the new consumer Applications, confirming each tracking owner and source revision. Create `k3s-postgres` with its original Cluster/Secret/backup identities. Disable the old `postgresql` reconciler during adoption.
4. Only after verified adoption, remove the old `postgresql`, `tls-certificates` and `letsencrypt-dns01` Applications **non-cascading**, for example `argocd app delete postgresql --cascade=false`. Never delete the Cluster, namespace, PVCs, database roles or KV entries as part of the rename. Never force-remove finalizers to get a green status.
5. Retire the legacy issuer's ServiceAccount, RoleBinding, Job and its narrowly named OpenBao policy/login role using an authorized administrator; account for any still-valid short-lived issuer tokens. Verify old ownership is gone before resuming automated reconciliation and final acceptance.

This source reorganization itself does not perform that live cutover or update a live root policy.
