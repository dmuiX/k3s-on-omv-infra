# Native chart capabilities and shared-resource audit

Date: 2026-10-27

## Current Infra follow-up: native workload identities

Reviewed all 16 external Infra chart pins, bundled monitoring charts, the five
local support charts and the companion Live Infra overlays (`e78a5c0`). This
follow-up is independent of Infra #66; Grafana's native reader-account reuse is
handled separately and is not included here. It does not enable new policies,
change replicas, replace specialized Jobs or introduce
`extraObjects` wrappers. The older audit below is a historical snapshot, not a
claim that its pending migrations are still pending in the adoption branches.

| Infra chart / version | Current native-first disposition |
| --- | --- |
| argo-cd 10.10.3 | Component accounts/policies already chart-owned; no duplicate reader account or equivalent raw policy to remove. |
| Istio base 1.31.2 | Native foundation resources; no duplicate consumer identity. |
| istiod 1.31.2 | Native account and optional policy; enabling additional isolation is separate hardening. |
| Istio CNI 1.31.2 | Native account; optional policy requires host-network/Ambient review, not a relocation. |
| ztunnel 1.31.2 | Native account; mesh infrastructure policies cannot replace consumer isolation. |
| prometheus-operator-crds 32.0.1 | CRDs only; no workload identity or policy. |
| plugin-barman-cloud 0.8.1 | Native operator account; no native operator NetworkPolicy/PDB replacement. |
| cert-manager v1.21.2 | **Reuse native `cert-manager` account for the DNS01 VSO reader; remove `cert-manager-dns01` ServiceAccount.** Native controller/webhook/cainjector policies have no corresponding raw duplicate here. |
| cloudnative-pg 0.29.1 | Native controller already reads both delivered Secrets; reuse its `cloudnative-pg` account in independent main-targeted PR #75. Instance identity is a separate lifecycle choice. Keep database policies and standalone operator PDB. |
| crossplane 2.4.2 | **Let DeploymentRuntimeConfig create the stable `provider-vault` account through `serviceAccountTemplate.metadata.name`; remove its standalone manifest and pod-level account override.** Keep the provider identity distinct from Crossplane's controller. |
| csi-driver-smb 1.20.3 | Native controller already consumes SMB credentials and has cluster-wide Secret get; reuse `csi-smb-controller-sa` in independent main-targeted PR #76. Keep the host R2 helper separate. No native NetworkPolicy template. |
| k8up 4.10.0 | Native operator account is not a backup/restore Job account. Keep those identities and standalone PDB; no native NetworkPolicy template. |
| longhorn 1.13.0 | Native internal policies and configured metrics sources already used. OIDC proxy already shares its workload/reader identity; credential producer remains separate. |
| vault-secrets-operator 1.6.0 | Native controller account; do not substitute it for every consumer's OpenBao identity. No native NetworkPolicy template. |
| openbao 0.30.4 | Native server policy/PDB already adopted. Snapshot and injector identities/policies have different scopes. Metrics-listener follow-up remains Infra #71. |
| kube-prometheus-stack 92.10.1 | **Reuse native `kube-prometheus-stack-alertmanager` account for the SMTP/config VSO reader; remove `alertmanager-vso` ServiceAccount.** Grafana reuse is handled separately in #66, not inherited here; provisioning and backup-Job identities remain separate. |

Bundled Grafana 14.0.1, kube-state-metrics 8.6.0, node-exporter 4.60.0,
windows-exporter 0.12.9 and CRDs 0.0.0 were included in the inventory. Their
component accounts do not replace unrelated provisioning/backup identities.
Grafana/KSM/node-exporter policy selectors and rules differ from the remaining
raw policies. Local `service-route`, `openbao-oidc`, `oidc-proxy`,
`postgres-consumer` and unused Infra `workload-network` charts need no new
identity abstraction. Moving raw objects into a new chart source only to reduce
file count is not a simplification.

All 11 remaining raw NetworkPolicies are retained: OpenBao namespace isolation,
injector and snapshots (3); PostgreSQL instance isolation (2); credential Jobs
(3); restore validation (3). None has a verified equivalent native replacement
in its associated chart. In particular, Grafana's native selector does not cover
its credential Job, and the CNPG operator chart does not manage instance policy.

### Identity migration and acceptance

The reader changes remove two ServiceAccount manifests and update their
VaultAuth/OpenBao-role account references. VaultAuth/role names, audience settings, TTLs, policies, KV
paths, Secret names/ownership and bootstrap ordering stay unchanged. The reader
roles now authorize the existing native workload identities for their respective
credential paths; this is an intentional trust-boundary change, not just YAML
movement. No producer/create permission is added to either workload.

cert-manager's controller account exists before the later DNS01 credential
Application. Alertmanager's account is rendered even while its CR has zero
replicas; this change must not start Alertmanager. Keep native account creation
and the owning chart component enabled while these VSO readers depend on them.

With pruning disabled, old accounts remain if previously deployed. After a
separately approved sync, require fresh VSO authentication as the new identity
(not only Ready from a cached old token), successful DNS01/config Secret delivery
and unchanged controller health. Only then consider attended retirement of the
old ServiceAccounts, accounting for outstanding tokens and other references.
Changing allowed account names does not itself revoke existing tokens. Do not
delete the still-used VaultAuth/OpenBao roles named `cert-manager-dns01` and
`alertmanager-vso`, delete Secrets, or force recreation of bootstrap credentials.
Rollback restores both identity references together and retains the old account
until acceptance; after retirement, restoring that account requires approval.

### Remaining standalone accounts

- Keep real workload identities `longhorn-oidc-proxy`, `openbao-snapshot`,
  `postgresql-size-retention` and `backup-validation`; these are not duplicate
  reader accounts. The proxy and snapshot workloads already reuse their reader
  identities.
- Keep `longhorn-credentials`, `k3s-postgres-credentials` and `grafana-credentials`
  separate from the read-only consumers because they create credentials.

### Complete follow-up on the five additional VSO readers

The following supersedes the earlier claim that no suitable SMB/PostgreSQL
reader had been established. Different controller purposes alone are not proof
that identity reuse is unsuitable. Effective existing Secret permissions matter.
This is a source-derived audit, not a live RBAC or secret-content inventory.
The two implementation PRs target **main directly**, independently of this PR.

| Reader | Verified existing candidate | Decision |
| --- | --- | --- |
| `postgresql-vso` | `cloudnative-pg/cloudnative-pg`, native controller, already reads both destination Secrets | Reuse in [#75](https://github.com/dmuiX/k3s-on-omv-infra/pull/75). |
| `smb-vso` | `kube-system/csi-smb-controller-sa`, actual provisioner credential consumer with unrestricted Secret get | Reuse in [#76](https://github.com/dmuiX/k3s-on-omv-infra/pull/76). |
| `platform-r2-vso` | SMB controller/helper are technically usable, but neither is the host k3s process | Retain; sharing is possible with an explicit union of upstream authorities. |
| `k8up-validation-vso` | Existing `backup-validation` or operator-created local `pod-executor` | Retain; either choice adds a source permission not directly supplied to that workload today. |
| `prometheus-k8up-vso` | Native monitoring accounts or local `pod-executor` | Retain pending an intentional sharing choice; several native accounts already read the destination Secret. Reuse is not technically blocked. |

**Common mechanics and permission delta.** VSO 1.6.0 retrieves the named account
and requests its token in the consuming Secret resource's namespace. A workload
need not run under that account for VSO to use it, and disabled token automount
does not prevent TokenRequest. A remote controller account is not a drop-in
replacement for a namespace-local reader. See the pinned
[Kubernetes credential provider](https://github.com/hashicorp/vault-secrets-operator/blob/v1.6.0/credentials/vault/kubernetes.go)
and [TokenRequest helper](https://github.com/hashicorp/vault-secrets-operator/blob/v1.6.0/helpers/serviceaccount.go).

Every reuse adds direct OpenBao login, source/metadata reads and renewal to the
chosen identity. That is not permission-neutral, even if it already reads the
materialized Secrets. Source KV records may contain omitted fields or historical
versions. The five current roles author no explicit audience restriction; ordinary
API-audience workload tokens may qualify for OpenBao login depending on backend
configuration. #75/#76 preserve those settings rather than bundling hardening.

**1. PostgreSQL.** `07-k3s-postgres/resources/credentials.yaml` delivers
`openbao-manager-credentials` and `postgresql-r2-credentials`. The manager is
consumed by the Cluster's managed `openbao_manager` role and provider-vault's
`SecretBackendConnection/postgresql`; R2 is consumed by the Barman ObjectStore
integration and `postgresql-size-retention` through envFrom. Actual ObjectStore
creation remains external Bootstrap configuration, not a live verified object.

Exact chart 0.29.1 (CNPG 1.30.1) gives the controller cluster-wide Secret
create/delete/get/list/patch/update/watch without resource-name restrictions.
It already exists at controller wave 2, before PostgreSQL wave 7. It is therefore
a stronger candidate than an arbitrarily chosen nearby workload. The Barman
controller also already reads these Secrets, but tying manager delivery to the
optional backup-plugin lifecycle is less appropriate. The instance account
`k3s-postgres` has named manager/plugin Secret access, but is created with the
Cluster and adds unnecessary instance-lifecycle coupling. The retention account
receives R2, not the manager input; the producer combines create authority; the
provider-vault account is in another namespace. #75 preserves role/policy names,
manager/R2 scopes (including the existing PostgreSQL metadata wildcard), token
settings, destinations and the separate producer identity.

Evidence: pinned chart `templates/rbac.yaml` and `_helpers.tpl`,
[CNPG managed-role Secret rules](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/pkg/specs/roles.go),
[Barman instance bindings](https://github.com/cloudnative-pg/plugin-barman-cloud/blob/v0.15.1/internal/cnpgi/operator/specs/role.go),
`07-k3s-postgres/resources/{cluster,credentials,retention}.yaml`.

**2. SMB.** Exact chart 1.20.3 gives `csi-smb-controller-sa` cluster-wide Secret
get. Its node account also has Secret get under the enabled inline-volume
feature; it is not Secret-unprivileged. Both already can retrieve the known SMB
and etcd Secret names. Live's `infra/csi-driver-smb/values.yml` uses
`kube-system/smb-credentials` for provisioner and node-stage credentials; the
static PostgreSQL mirror PV uses the same node-stage Secret. These are real
controller/node consumers, while application Pods mount CSI volumes without
needing SMB OpenBao rights. Prefer the controller over extending upstream login
to the distributed node identity. #76 keeps the role SMB-only and does not grant
R2 OpenBao access. Native controller installation precedes the credential link.

Evidence: pinned `templates/{rbac-csi-smb,csi-smb-controller,csi-smb-node}.yaml`,
[controller](https://github.com/kubernetes-csi/csi-driver-smb/blob/v1.20.3/pkg/smb/controllerserver.go)
and [node](https://github.com/kubernetes-csi/csi-driver-smb/blob/v1.20.3/pkg/smb/nodeserver.go),
Live's StorageClass values and `08-backup-validation/resources/validation.yaml`.

**3. Host etcd/R2.** `platform-r2-vso` delivers `kube-system/etcd-s3-config-secret`.
The configured host k3s server reads it through its runtime Kubernetes client;
it is not a Pod running as this helper. The configured k3s version is
`v1.37.1+k3s1`. There is no matching Helm-workload account. The local SMB
controller/node accounts already have Kubernetes get access to this Secret, so
reuse would not introduce first access to its current materialized values.
It would nevertheless add upstream R2-path/metadata authorization to an SMB
workload. One helper could instead authenticate to both existing SMB/R2 roles,
but its effective authority becomes their union; that is an alternative to #76,
not an additional independent simplification. Preserve separate upstream login
and revocation boundaries by default. Never substitute namespace `default`.

Evidence: Bootstrap `roles/k3s_server/templates/config.yaml.j2`,
[k3s Secret lookup](https://github.com/k3s-io/k3s/blob/v1.37.1%2Bk3s1/pkg/etcd/s3/config_secret.go),
[S3 runtime client](https://github.com/k3s-io/k3s/blob/v1.37.1%2Bk3s1/pkg/etcd/s3/s3.go)
and [server context](https://github.com/k3s-io/k3s/blob/v1.37.1%2Bk3s1/pkg/server/context.go).
CoreDNS/metrics-server have no comparable declared Secret grants; local-path and
Traefik are disabled. k3s controller User identities are not reusable Pod SAs.

**4. Validation backups.** This reader delivers both `k8up-repository` and
`postgresql-validation-r2` in `backup-validation`. K8up-generated Backup/Check/
Prune Jobs consume the repository password; the PostgreSQL validation CronJob
receives R2 through envFrom and creates a recovery ObjectStore selecting its
R2 Secret. The existing `backup-validation` account can manage validation
Clusters/ObjectStores and has named source-Secret permissions, but has no direct
repository-Secret read grant or repository-password input. Rebinding the combined
reader role to it adds direct repository-password source authorization as well
as R2 authorization. This observation is not a proof against indirect access
through its existing controller/resource-creation powers.

The actual K8up 4.10.0 operator image is v2.16.0. Its Backup/Prune default to
namespace-local `pod-executor`; Check leaves the SA unset, so Kubernetes uses
`default`. Live's two PodConfigs do not override it. PodConfig Spec is merged
using `mergo.WithOverride`, so explicit Job-SA alignment is supported, including
Check. However, the operator creates the configured `pod-executor` account and
executor RoleBinding independently of that override. Custom Job SAs do not
therefore inherit executor RBAC. This role permits Pods get/list, pods/exec
create/get and K8up Snapshots operations, not Secret reads. Initial account
existence depends on Backup reconciliation; choosing it for VSO couples initial
delivery to that lifecycle. Declaratively seeding another account or adding
bindings/token mounts is an orchestration change, not a free native substitution.
Using the existing reader as a Job identity can be valid, but does not remove
that account and gives Jobs the combined R2/repository source scope. Do not
change the operator's global account flag merely for these two namespaces.

Evidence: `08-backup-validation/resources/{credentials,validation,openbao-access}.yaml`,
Live `infra/backup-validation/{targets,k8up-omv}.yaml`, K8up v2.16.0
[Job merge](https://github.com/k8up-io/k8up/blob/v2.16.0/operator/job/job.go),
[executor identity creation](https://github.com/k8up-io/k8up/blob/v2.16.0/operator/backupcontroller/backup_utils.go),
[Backup](https://github.com/k8up-io/k8up/blob/v2.16.0/operator/backupcontroller/executor.go),
[Prune](https://github.com/k8up-io/k8up/blob/v2.16.0/operator/prunecontroller/executor.go)
and [Check](https://github.com/k8up-io/k8up/blob/v2.16.0/operator/checkcontroller/executor.go).

**5. Prometheus backup repository.** The only delivered Secret is
`prometheus-k8up-repository`, consumed by one-shot K8up Backup/Check/Prune resources
from `docs/prometheus-tsdb-backup-runbook.md`. The Prometheus server, TSDB snapshot
PreBackupPod and cleanup Job do not reference that password Secret. There is no
Prometheus K8up Schedule in the inspected Live directory; its PodConfig/PVC
support those attended operations. Password injection is via secretKeyRef, not
a Secret-get call by the Job account.

Several existing native monitoring accounts nevertheless already read it:
Grafana has cluster-wide Secret get/list/watch; the Prometheus operator has
get/list/watch/create/update/delete; kube-state-metrics has list/watch, which
exposes full Secret objects rather than metadata alone. These are technically
viable substitutions; denying that they already see the password would be
incorrect. None is the actual K8up Job consumer. Direct OpenBao authorization
would additionally couple backup custody to that component. Prometheus's own
account has no corresponding Secret-read rule, so it gains additional authority.
Alertmanager likewise has no matching Secret rule. Admission/test hook accounts
are lifecycle-bound, node-exporter and the K8up controller are in other namespaces,
and `pod-executor` has the creation/binding considerations described above.
Retaining the helper is an explicit isolation/lifecycle choice, not a technical
impossibility. Sharing with an existing reader remains an available alternative
if reducing object count takes priority over a separate upstream identity.

Evidence: pinned monitoring chart 92.10.1 `prometheus/clusterrole.yaml`,
`prometheus-operator/clusterrole.yaml`, bundled Grafana ClusterRole and KSM Role/
ClusterRoleBinding; `09-kube-prometheus-stack/resources/{prometheus-k8up-credentials,
prometheus-snapshot}.yaml`, Live `infra/kube-prometheus-stack/prometheus-k8up-omv.yaml`,
and the TSDB backup runbook. Chart defaults and Live inputs are included in the
permission analysis; live drift, extra bindings, backend audiences, source fields
and actual external bucket permissions remain unverified.

The two implemented cases ran only one exact-chart test each (0.742s and 0.145s),
including effective existing Secret-read RBAC and unchanged reader policy checks.
All other follow-up work was source inspection; no broad suites, live operations,
secret-value retrieval, mounts or backups were performed.

### Native provider-account ownership

Crossplane 2.4.2 supports a named `serviceAccountTemplate`. Its runtime builder
uses that name for both the account and provider Deployment; ProviderHooks applies
the account before the Deployment. Previously, the explicit pod account override
selected an externally managed account instead. Keep the stable name/namespace,
provider package pin, runtime hardening, Kubernetes/OpenBao permissions and token
mount behavior unchanged; only account creation/lifecycle ownership moves.

This is an attended ownership handoff if the provider already exists. Pruning is
disabled, so do not delete the old account: Crossplane must adopt the same object
without changing its UID. Verify provider health, runtime account selection,
Kubernetes/OpenBao authentication and owner references before acceptance. Native
accounts are now owned by ProviderRevisions; provider uninstall/revision cleanup
must respect that lifecycle. Source rollback alone does not remove controller
owner references. Reverting to external management requires a separately reviewed
handoff, not deleting the Provider/account or blindly stripping owner references.

Evidence: pinned [runtime-config API](https://github.com/crossplane/crossplane/blob/v2.4.2/apis/pkg/v1beta1/deployment_runtime_config_types.go),
[runtime builder](https://github.com/crossplane/crossplane/blob/v2.4.2/internal/controller/pkg/runtime/runtime.go)
and [ProviderHooks/account adoption](https://github.com/crossplane/crossplane/blob/v2.4.2/internal/controller/pkg/runtime/runtime_provider.go).
One additional static contract checks native template/account selection; no extra
chart render or live test was run for that source-wiring check.

Native evidence: pinned cert-manager `templates/serviceaccount.yaml` and
`deployment.yaml`; kube-prometheus-stack `templates/alertmanager/serviceaccount.yaml`
and `alertmanager.yaml`; bundled Grafana `serviceaccount.yaml`, `_pod.tpl` and
`networkpolicy.yaml`. One focused test with two subcases renders only the changed
charts with their actual Live inputs, checks workload/account/reader-role
agreement, exact reader ACLs and unchanged audiences, and verifies Alertmanager
remains at zero replicas. No full suite or live test was run for this follow-up.

## Original audit scope and decision (2026-10-22)

The original audit inspected the Infra, Apps and Live working trees at that date, including then-uncommitted Radar changes. Inspected the exact 17 external chart versions referenced by Applications, bundled subcharts where relevant, and the local Audiobookshelf chart 0.1.0. Inventory covers authored resource families and consolidation opportunities, not every possible combination of every chart value. No cluster state, installed field ownership, or runtime behavior was inspected.

**Recommendation:** use existing chart values and operator APIs first. Manage destination-namespace metadata in one designated Application. Use small reusable Helm components for repeated, parameterized platform integrations; retain plain YAML/Kustomize for deployment-specific resources and overlays. Do not convert everything to one universal chart.

That original audit created no deployment changes. Previously requested Radar changes in Apps/Live remain separate from this report.

## Findings that change the implementation plan

1. OpenBao already renders a native server PDB with the **same identity** as our standalone PDB. Remove the duplication only after explicitly preserving the fixed availability budget and reviewing the selector change.
2. Barman Cloud, CloudNativePG and K8up have **no native operator PDB values** in the pinned charts. Their three standalone PDBs cannot simply be converted to typed upstream settings.
3. Eleven authored Namespace objects are straightforward candidates for Application metadata. `argocd` and `kube-system` need separate adoption/ownership decisions; `node-exporter` must remain explicit because it is not its Application's destination namespace.
4. Longhorn's native HTTPRoute targets the frontend directly, not our authentication proxy. Replacing the current route would bypass that proxy.
5. OpenBao's pinned HTTPRoute template omits API-defaulted backend `group` and `weight` and does not offer a full rules override. Unknown values cannot fix this.
6. Native VSO connection and transformation sharing should be evaluated before building a custom secrets framework.
7. K8up's configured root `leaderElection: true` is unused. The chart reads `k8up.enableLeaderElection`, currently true by default. This is a configuration-cleanup finding, not evidence that leader election is disabled.

## 1. PDB support across all configured charts

A PDB controls voluntary eviction, not all downtime. It does not make one replica highly available, protect against node failure, or guarantee safe Deployment upgrades. Adding new budgets is an availability-policy change, distinct from migrating existing budgets.

| Chart/version | Native PDB values | Current state and action |
| --- | --- | --- |
| argo-cd 10.10.3 | `{controller,dex,redis,server,repoServer,applicationSet,notifications}.pdb` | Disabled. Set `enabled` and an explicit availability target; `enabled: true` alone defaults to `minAvailable: 0`. Redis HA has separate map-style settings. |
| Istio base 1.31.2 | None | CRDs/control-plane foundation, no workload budget to migrate. |
| Istiod 1.31.2 | `global.defaultPodDisruptionBudget.enabled`, `pdb.{minAvailable,maxUnavailable,unhealthyPodEvictionPolicy}` | Rendering also requires `autoscaleMin > 1`, or fixed replicas > 1 with autoscaling disabled. Current configuration renders no PDB. |
| Istio CNI 1.31.2 | None | DaemonSet; do not invent a Deployment-style availability guarantee. |
| Ztunnel 1.31.2 | None | DaemonSet; no PDB template. |
| prometheus-operator-crds 32.0.1 | None | CRDs only. |
| plugin-barman-cloud 0.8.1 | None | Keep authored `cloudnative-pg/barman-cloud`, min 1, replicas 2. Deployment template hardcodes `Recreate`; the PDB does not prevent upgrade downtime. |
| cert-manager v1.21.2 | `podDisruptionBudget`, `webhook.podDisruptionBudget`, `cainjector.podDisruptionBudget` | Already used: three native budgets, min 1, two replicas each. No migration needed. |
| cloudnative-pg 0.29.1 | None for the operator | Keep authored `cloudnative-pg/cloudnative-pg-controller`, min 1, replicas 2. Database-instance PDBs are separately operator-managed. |
| crossplane 2.4.2 | None | `extraObjects` can carry raw YAML but is not typed PDB support. Main controller/RBAC manager currently single-replica. |
| csi-driver-smb 1.20.3 | None | No dedicated PDB template. Controller and node DaemonSets have different availability semantics. |
| k8up 4.10.0 | None | Keep authored `k8up/k8up`, min 1, replicas 2. |
| longhorn 1.13.0 | `longhornUI.podDisruptionBudget`, `longhornGlobalManager.podDisruptionBudget` | Require `enabled: true` and corresponding replicas > 1. UI has two replicas and is an adoption candidate. These are not Longhorn's dynamically managed engine/CSI budgets. |
| vault-secrets-operator 1.6.0 | `controller.podDisruptionBudget.{enabled,minAvailable,maxUnavailable}` | Requires controller replicas > 1. Current replicas 1 render no PDB. Configure only one availability field. |
| openbao 0.30.4 | `server.ha.disruptionBudget.{enabled,maxUnavailable}`; `injector.podDisruptionBudget` | Server budget already enabled implicitly. Make max 1 explicit before removing authored duplicate. Injector accepts a map, not an `enabled` flag. |
| kube-prometheus-stack 92.10.1 | `{prometheus,alertmanager,thanosRuler,prometheusOperator}.podDisruptionBudget`; `prometheusOperator.admissionWebhooks.deployment.podDisruptionBudget` | Disabled. Prometheus/Alertmanager have two replicas: candidates for deliberate adoption. Defaults include min 1 and `AlwaysAllow`; review rather than silently accepting new policy. |
| Bundled Grafana 14.0.1 | `grafana.podDisruptionBudget` | Already used, min 1. Map-style, no `enabled` key. |
| Bundled kube-state-metrics 8.6.0 | `kube-state-metrics.podDisruptionBudget` | Map-style, currently unset. |
| radar 1.17.0 | None | One replica intentionally; PostgreSQL timeline storage does not make Radar multi-replica. Do not add min 1 blindly. |
| Audiobookshelf local 0.1.0 | None currently | Local chart can be extended if a real eviction policy is required. One replica/SQLite means min 1 can block drains. |

### Exact existing-PDB migration result

There are four standalone PDBs:

- `02-controllers/barman-cloud/resources/availability.yaml`
- `02-controllers/cloudnative-pg/resources/availability.yaml`
- `02-controllers/k8up/resources/availability.yaml`
- `04-openbao/resources/security.yaml`

Only the last has a native replacement. The authored OpenBao selector is `app.kubernetes.io/name: openbao`. The chart adds `app.kubernetes.io/instance: openbao` and `component: server`. Native and authored objects both have identity `policy/v1 PodDisruptionBudget openbao/openbao`; Argo's later source currently overrides the native object. This is duplicate desired configuration of one object, not two differently named PDBs.

The other three selectors omit the Helm instance label. Tightening selectors may be worthwhile but is a separate behavior change, not required to move files.

## 2. Namespace metadata belongs in Applications, with exceptions

Use `spec.syncPolicy.managedNamespaceMetadata` and `CreateNamespace=true`. Preserve every existing label; privileged namespaces must not silently become restricted or vice versa.

Abbreviations: **R/P** = PSA enforce restricted/privileged; **WA** = PSA warn and audit restricted; **A** = Istio Ambient.

| Namespace | Existing metadata | Designated Application | Disposition |
| --- | --- | --- | --- |
| istio-gateway | R | letsencrypt-dns01-issuer | Move from `00-letsencrypt-dns01-issuer/resources/namespace.yaml`; retain early ordering. |
| argocd | R, WA | argocd-config | Conditional. Authored `01-bootstrap/argocd-config/resources/namespace.yaml` is not included by current Application sources. Adding management is adoption, not just relocation. Add `CreateNamespace=true` after reviewing ownership. |
| istio-system | P | istio-core | Move from `01-bootstrap/istio/base-resources/namespace.yaml`; not also to istio-ambient. |
| cert-manager | R, A | cert-manager | Move from controller resources. |
| cloudnative-pg | R, A | cloudnative-pg | Remove only Namespace document from `availability.yaml`; other consumers must not compete. |
| crossplane-system | R, A | crossplane | Move from controller resources. |
| kube-system | P, WA | Explicit decision required | Current manifest is in csi-driver-smb resources; Application lacks `CreateNamespace=true`. Shared Kubernetes system namespace: do not adopt automatically. |
| k8up | R, A | k8up | Remove only Namespace document from `availability.yaml`. |
| longhorn | P, WA | longhorn | Preserve privileged requirements, not restricted defaults. |
| vault-secrets-operator | R, A | vault-secrets-operator | Move from controller resources. |
| openbao | R, A | openbao | Remove only Namespace document from `security.yaml`. |
| backup-validation | R, A | backup-validation | Remove only Namespace document from `validation.yaml`. |
| kube-prometheus-stack | R, A | kube-prometheus-stack | Remove only Namespace document from `grafana.yaml`; CRD Application is an earlier consumer. |
| node-exporter | P, WA; sync wave -20 | Explicit Namespace in monitoring resources | Keep: chart uses `namespaceOverride: node-exporter`, but Application destination is kube-prometheus-stack. |
| radar | R, A | radar | Already implemented. |
| audiobookshelf | P, WA, A | audiobookshelf | Already implemented. Preserve local-media admission prerequisite and disabled automated sync. |

There are 14 authored Namespace objects in Infra, none in Apps/Live. Eleven are straightforward migration candidates, two conditional, one required exception.

[Argo CD v3.5.5 namespace metadata documentation](https://github.com/argoproj/argo-cd/blob/v3.5.5/docs/user-guide/sync-options.md#namespace-metadata) establishes:

- Only the Application destination namespace is handled, and `CreateNamespace=true` is required.
- An explicit Namespace manifest takes precedence over managed metadata. Retaining both does not complete migration.
- Existing client-side-applied namespaces require careful SSA adoption; setting SSA on an Application does not prove current field ownership.
- Generated namespaces normally are not resource-tracked in this version. Manually adding tracking can introduce deletion risk; do not do this as part of metadata cleanup.
- Do not prune/delete a namespace while transferring its metadata management. Existing `prune: false` is helpful but not protection against every manual/future prune operation.

The older files are an inconsistent ownership convention, not evidence that native namespace metadata is unavailable. Runtime adoption safety is the reason not to replace them blindly now.

## 3. Other native settings worth using

The following distinguishes supported capabilities from recommendations to enable them. A monitoring endpoint, route or network policy must still satisfy the actual security and discovery requirements.

| Component/version | Verified native values/API | Assessment |
| --- | --- | --- |
| Argo CD 10.10.3 | `server.httproute.*`; component `metrics.enabled` / `metrics.serviceMonitor.enabled`; `configs.rbac.policy.csv`; `global.networkPolicy.create` | Native route can replace Live route after name migration (`argocd` to chart's `argocd-server`) and verification of HTTPS backend. Retain BackendTLSPolicy. Six native NetworkPolicies already render, but are not equivalent to default-deny isolation. |
| Istio 1.31.2 | istiod/CNI/ztunnel `global.networkPolicy.enabled`; native workload resources, scheduling, security settings | NetworkPolicy templates exist but currently render none. Inspect Ambient, host networking and API traffic before enabling. Gateway workload customization already uses native `infrastructure.parametersRef` and ConfigMap, not a missing Helm feature. |
| Barman Cloud 0.8.1 | RBAC, ServiceAccount, certificate/issuer, resources and scheduling | Already chart-owned. No native NetworkPolicy, route or monitoring-resource templates. Do not rely on documented update-strategy values when deployment template hardcodes `Recreate`. |
| cert-manager v1.21.2 | `{networkPolicy,webhook.networkPolicy,cainjector.networkPolicy}.{enabled,ingress,egress}`; `prometheus.servicemonitor.enabled` or `prometheus.podmonitor.enabled` | Native monitoring and network policy candidates. Monitor options are mutually exclusive. Gateway API support configures certificate handling, not an application HTTPRoute. |
| CloudNativePG 0.29.1 | `monitoring.podMonitorEnabled`, `monitoring.podMonitorAdditionalLabels`, `monitoring.grafanaDashboard.create` | Native operator monitoring. Prometheus PodMonitor discovery labels must match; ServiceMonitor discovery configuration alone does not cover PodMonitors. |
| Crossplane 2.4.2 | `metrics.enabled`, `rbacManager.*`, package/function cache existing PVC references | Metrics setting exposes metrics but does not create a ServiceMonitor. Existing PVC references do not create storage. Provider workload configuration belongs in `DeploymentRuntimeConfig`. |
| SMB CSI 1.20.3 | `storageClasses[]`: name, annotations, parameters, reclaim/binding policies, expansion, mount options | Concrete migration candidate for Live SMB StorageClass. Preserve `smb` name, Retain, mount options and Secret references. Never recreate a bound storage class casually. |
| K8up 4.10.0 | `metrics.serviceMonitor.enabled`, `metrics.prometheusRule.enabled`, `metrics.prometheusRule.additionalRules`, `metrics.grafanaDashboard.enabled`, `k8up.globalResources`, `k8up.enableLeaderElection` | Monitoring and backup-job defaults can use native values. Schedules and repository bindings remain native K8up CRs. |
| Longhorn 1.13.0 | `metrics.serviceMonitor.enabled`, `networkPolicies.metricsScrapeSources`, `defaultSettings.*`, `csi.*` | Six internal policies already render through default `networkPolicies.restrictInternalTraffic: true`, independent of `networkPolicies.enabled: false`. Native route cannot target the OIDC proxy. Native single StorageClass does not cover our three encrypted classes. |
| VSO 1.6.0 | `controller.podDisruptionBudget`, `telemetry.serviceMonitor`, `defaultVaultConnection`, `defaultAuthMethod` | Connection/auth creation is deliberately disabled today. Evaluate shared connection separately; do not turn on a shared broad identity as a shortcut. |
| OpenBao 0.30.4 | `server.networkPolicy.{enabled,ingress,egress}`; `serverTelemetry.serviceMonitor`, `serverTelemetry.prometheusRules`, `serverTelemetry.grafanaDashboard`; HA/storage; `snapshotAgent` | Server policy is a partial migration candidate, not replacement for namespace default deny, snapshot-job or injector rules. Injector chart policy is OpenShift-gated. SnapshotAgent does not establish equivalence with existing retention/checksum contracts. |
| Monitoring stack 92.10.1 | `additionalPrometheusRulesMap`; component network policies and `route.main`; native storage specs; `kube-state-metrics.rbac.extraRules` | Custom alert rules can move into values, but generated resource name changes. Grafana native route, external database configuration, PDB and stack storage are already used. Enabling policies requires scrape/webhook/mesh checks. |
| Radar 1.17.0 | `httpRoute.rules`, granular `rbac.*`, OIDC/existing Secrets, PostgreSQL timeline, Argo integration | Already uses these values. No native NetworkPolicy/PDB/ServiceMonitor. Human `radar:admin` ClusterRoleBinding is not equivalent to chart service-account RBAC. |
| Audiobookshelf local 0.1.0 | `httpRoute`, `networkPolicy`, `oidc`, `persistence`, media configuration, workload resources | Already packages these app-specific functions. No PDB value currently; introducing one is a local feature, not an upstream switch. Preserve SQLite/single-replica and local-media safety controls. |

### HTTPRoute defaults must survive rendering

Every Gateway parent reference must have explicit `group: gateway.networking.k8s.io` and `kind: Gateway`. Every Service backend needs explicit `group: ""`, `kind: Service` and `weight`.

- Radar custom `httpRoute.rules`, Grafana and Audiobookshelf satisfy backend defaults.
- Argo CD's native route template supports them; still verify full rendered output and resource names.
- OpenBao 0.30.4's native backend omits group/weight with no full rules override. Use an explicit route or an upstream-supported fix; do not pretend arbitrary additional values are consumed.
- Longhorn's native route hardcodes `longhorn-frontend:80`; our route must continue pointing at `longhorn-oidc-proxy`.

## 4. Native operator reuse before custom charts

VSO 1.6.0 already supports:

- Namespace-qualified `VaultAuth.spec.vaultConnectionRef` for connection reuse.
- `VaultAuthGlobal` for shared defaults, with allowed-namespace controls.
- `SecretTransformation` plus `transformationRefs`, including output-key mapping.

These can reduce repeated connection objects and identical R2/OIDC transformations without sharing consumer credentials or permissions. Keep each workload's ServiceAccount, OpenBao policy, auth role, secret paths and restart behavior explicit. Native sharing still needs trust-boundary review and deliberate lifecycle ownership.

CloudNativePG Cluster/Database, Barman ObjectStore/ScheduledBackup, K8up Schedule/PodConfig, Crossplane DeploymentRuntimeConfig and Istio Gateway infrastructure parameters are existing APIs. A chart can package them, but is not a replacement controller.

## 5. What should remain a component or Kustomize?

| Resource family | Evidence of reuse | Recommended mechanism |
| --- | --- | --- |
| OpenBao OIDC client integration | Radar, Audiobookshelf, Longhorn | Small reusable Helm chart is justified if adopted for these consumers together. Parameterize assignments, claims, TTLs, client/provider/key names and Secret delivery. Do not create platform-owned profile scopes repeatedly. Longhorn's proxy remains separate. |
| OpenBao consumer access and Secret delivery | Many Infra/App consumers | Native VSO sharing first; then a narrowly scoped Helm component only for genuinely repeated policy/auth/delivery wiring. No shared superuser identity. |
| PostgreSQL consumer | Radar and Grafana | Strong Helm-component candidate: Database, OpenBao static role and Secret delivery. Preserve DSN versus split username/password outputs and rotation ownership. |
| Network rules | Many consumers | Native chart rules when equivalent; reusable explicit DNS/Gateway/OpenBao-client patterns where useful. Parameterize selectors/ports and preserve namespace-wide versus workload-only isolation. No universal allowlist. |
| HTTPRoute without suitable upstream support | Longhorn proxy and potential OpenBao explicit route | Small route component can serve repeated cases; app-specific BackendTLSPolicy remains explicit. Do not duplicate an already suitable upstream route. |
| Standalone controller PDBs | Barman, CloudNativePG, K8up | Reasonable tiny shared Helm component if component consolidation proceeds; otherwise these three short plain manifests are already simple. Never label `extraObjects` as native typed PDB support. |
| Local K8up repository PVC/PodConfig | Backup-validation and Prometheus | Possible small reusable storage bundle. Keep backup execution/verification separate. |
| Specialized jobs | Credential creation, snapshots, retention, restore validation, ABS API configuration | Preserve implementation and least-privilege permissions. Existing `adapters/credential-custody` already shares transport/create-once mechanics. Similar Job envelopes do not imply equivalent behavior. |
| Static PV, admission policy, BackendTLSPolicy, issuer/provider bootstrap, unusual StorageClasses | Infrastructure-specific contracts | Plain YAML/Kustomize is appropriate; no demonstrated need for a generic chart. |

**Helm versus Kustomize rule:** use Helm when repeated consumers need a stable values interface; use Kustomize when applying a small environment-specific patch to concrete objects. Do not build both interfaces for the same purpose. An independently rendered upstream Helm source cannot consume a library chart automatically; a reusable support chart must be an explicit source/dependency.

`extraObjects`/`extraDeploy` only relocate raw manifests into values. They do not eliminate custom logic, provide typed domain support, or resolve nested template escaping. Prefer them only when manifest colocation is itself a useful, verified simplification.

## 6. Safe rollout order

1. Implement the eleven straightforward namespace metadata moves, preserving exact labels and one steward per namespace. Review existing namespace SSA/tracking before live adoption; retain the node-exporter exception.
2. Remove the OpenBao duplicate PDB, explicitly preserving maxUnavailable 1 and accepting the server-specific selector. Keep the other three PDBs until their shared-component decision is made.
3. Correct the K8up leader-election value path. This does not require changing replica policy.
4. Migrate small proven native candidates: SMB StorageClass, then Argo route/custom monitoring rules with explicit name/ownership handling. Resolve OpenBao HTTPRoute defaults.
5. Evaluate availability additions (Longhorn UI, Prometheus, Alertmanager) separately from file cleanup. Do not increase replicas or block singleton drains implicitly.
6. Introduce one shared integration at a time, with rendered-equivalence tests across its consumers. Keep private values in Live and public implementation in Infra/Apps.

For each migration compare resource identities, selectors, rules, Secret references, labels, sync waves, storage retention and HTTPRoute defaults. With `prune: false`, removing a manifest does not remove an old differently named object. Do not manually delete security controls or namespaces as part of an automatic cleanup. Roll back repository changes and re-render before any reviewed sync; ownership-changing live rollback needs its own plan.

## Evidence and verification

Inspected the exact cached chart archives or downloaded the exact missing versions from the Application's configured official repository. Templates and values were read, not just chart README examples. All 17 upstream chart sources were rendered locally with current public values (Radar with its Live values). Bundled Grafana settings include private overrides in the targeted PDB check. No generated Secrets were published in this report. This is not a full private-overlay integration test for every Application, and does not establish deployed state.

Current native-PDB renders: three cert-manager budgets, one Grafana budget and one OpenBao budget. The other external chart sources rendered no PDB with their current inputs. Absence in current output is distinct from absence of configurable chart support.

Primary sources (versioned templates/values take precedence over generic docs):

- [Argo CD 10.10.3 chart](https://github.com/argoproj/argo-helm/tree/argo-cd-10.10.3/charts/argo-cd): component `pdb.yaml`, server `httproute.yaml`, `values.yaml`.
- [Istio 1.31.2 charts](https://github.com/istio/istio/tree/1.31.2/manifests/charts): istiod PDB and istiod/CNI/ztunnel NetworkPolicy templates; official archive source `https://blob.istio.io/istio-release/charts`.
- [Prometheus charts](https://github.com/prometheus-community/helm-charts): release tags `kube-prometheus-stack-92.10.1` and `prometheus-operator-crds-32.0.1`; stack values, PDB/rules templates and bundled Grafana/KSM charts.
- [CloudNativePG charts](https://github.com/cloudnative-pg/charts): `cloudnative-pg` 0.29.1 and `plugin-barman-cloud` 0.8.1 archives from `https://cloudnative-pg.github.io/charts`; deployment, monitoring and full template inventories.
- [cert-manager v1.21.2](https://github.com/cert-manager/cert-manager/tree/v1.21.2/deploy/charts/cert-manager): PDB, network policy and monitor templates.
- [Crossplane v2.4.2](https://github.com/crossplane/crossplane/tree/v2.4.2/cluster/charts/crossplane): values and templates, including `extra-objects.yaml`.
- [SMB CSI v1.20.3](https://github.com/kubernetes-csi/csi-driver-smb/tree/v1.20.3/charts/v1.20.3/csi-driver-smb): `templates/storageclass.yaml` and values.
- K8up chart 4.10.0 from the configured official repository `https://k8up-io.github.io/k8up`: deployment, metrics templates and values.
- [Longhorn 1.13.0](https://github.com/longhorn/charts/tree/longhorn-1.13.0/charts/longhorn): PDB, network policy, HTTPRoute and StorageClass templates.
- [VSO 1.6.0](https://github.com/hashicorp/vault-secrets-operator/tree/v1.6.0): chart `templates/poddisruptionbudget.yaml`; API `vaultauth_types.go`, `vaultauthglobal_types.go`, `common.go`.
- OpenBao chart 0.30.4 from `https://openbao.github.io/openbao-helm`: `templates/server-disruptionbudget.yaml`, `server-httproute.yaml`, `server-network-policy.yaml`, `injector-network-policy.yaml`, values. Archive SHA-256: `f9d464f0288a96374fcddb33abb31bc3f134ccaa32da3e4cf272f143b34bb96b`.
- [Radar v1.17.0](https://github.com/skyhook-io/radar/tree/v1.17.0/deploy/helm/radar): values, HTTPRoute template, full template inventory.
- Local Apps `audiobookshelf/chart/{Chart.yaml,values.yaml,templates/}` and `radar/resources/`; Live `apps/radar/values.yml` and OIDC overlay.
- [Kubernetes PDB behavior](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/), [Argo multiple-source precedence](https://argo-cd.readthedocs.io/en/stable/user-guide/multiple_sources/), and the versioned Argo namespace documentation linked above.
