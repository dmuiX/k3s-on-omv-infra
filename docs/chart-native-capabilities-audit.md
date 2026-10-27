# Native chart capabilities and shared-resource audit

Date: 2026-10-22

## Scope and decision

Audited the current Infra, Apps and Live working trees, including uncommitted Radar changes. Inspected the exact 17 external chart versions referenced by Applications, bundled subcharts where relevant, and the local Audiobookshelf chart 0.1.0. Inventory covers authored resource families and consolidation opportunities, not every possible combination of every chart value. No cluster state, installed field ownership, or runtime behavior was inspected.

**Recommendation:** use existing chart values and operator APIs first. Manage destination-namespace metadata in one designated Application. Use small reusable Helm components for repeated, parameterized platform integrations; retain plain YAML/Kustomize for deployment-specific resources and overlays. Do not convert everything to one universal chart.

The audit created no deployment changes. Previously requested Radar changes in Apps/Live remain separate from this report.

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
