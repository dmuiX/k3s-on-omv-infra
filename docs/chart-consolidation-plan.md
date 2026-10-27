# Chart consolidation implementation plan

Date: 2026-10-27
Status: draft publication of the prepared native/shared-chart adoption, ported onto current main plus the reviewed root/value-file prerequisites. Offline validation is not live acceptance. No live deployment is authorized by this document.

## Consolidated publication status (2026-10-25)

The authoritative cross-repository inventory and rollout gates are tracked in
[Bootstrap issue #62](https://github.com/dmuiX/k3s-on-omv-bootstrap/issues/62).
The original Infra working tree is based on `71389eb`, behind published main
`e4fe89e`; Apps is also behind its current main. Port the prepared changes onto
current main rather than publishing these stale files wholesale. Preserve the
already-merged PostgreSQL 20Gi request, Alertmanager zero replicas and Apps
image-tag version-label fix.

The agreed root direction is now public Infra Application definitions with
private Live Helm value files. The narrow published migration is Live PR #9,
Infra PR #63 and Bootstrap PR #61, in that dependency order; none of the broad
native/shared-chart work below is implicitly included. Align later local private
values with the canonical layout accepted by those PRs, rather than keeping
competing value-file paths. Retain the old Live wrapper until root cutover is
accepted. The earlier objective to preserve the private Kustomize root is
superseded; the remaining historical stages do not authorize a second root design.

Grafana admin credential custody (#59), current PostgreSQL disk-full recovery,
and normal-deploy MFA hardening (#60, deferred) are separate tracks. Existing
Live PR #4 is a supersession candidate; #5 has unresolved certificate/TLS pieces
and must not be called wholly superseded or merged unchanged.

## Draft publication update (2026-10-26)

The chart library is Infra PR #64, stacked on root PR #63. Native/shared consumer
adoption, private consumer values and Apps integration are separate coordinated
drafts tracked in Bootstrap issue #62. Provider access grants are Bootstrap
PR #64, stacked on the standalone admin/MFA maintenance PR #63. Grafana admin
custody is deliberately excluded from this platform adoption batch and remains
a separate dependent change under issue #59.

The direct public root and component-local private values are retained. Private
`values.yml` and `*-values.yml` files carry Argo CD's native skip-file-rendering
marker. Legacy root wrappers remain for rollback; their deletion in the original
working tree is not published in this adoption batch. The already-merged
PostgreSQL 20Gi, Alertmanager 0 and Audiobookshelf fixes are preserved.

The stage descriptions below are historical design notes. Where they mention
private Application JSON patches, the implementation now uses native external
Helm value files. Where they describe an upstream chart feature, the exact pinned
chart is rendered by the adoption tests. A fresh full differential review and
resource-by-resource live handoff approvals remain outstanding.

## Current implementation status

- Namespace metadata: twelve stewards including Argo CD; only kube-system/node-exporter remain explicit.
- Native configuration: K8up leader election, Argo route/metrics, cert-manager/K8up/Longhorn monitors, CNPG operator PodMonitor, SMB StorageClass, with the existing standalone platform alert definitions retained.
- PDBs: OpenBao duplicate retired in favor of explicit native maxUnavailable 1; separately approved Longhorn UI/Prometheus/Alertmanager native minAvailable 1 enabled, existing two replicas unchanged.
- VSO: native authenticated HTTPS ServiceMonitor enabled; chart-hardcoded HTTPS certificate verification skip explicitly accepted with Ambient mTLS.
- OpenBao: native server NetworkPolicy carries the exact existing rule arrays; intended release-specific selector and changed policy identity are documented for adoption. Default-deny/injector/snapshot exceptions remain explicit.
- OpenBao metrics are excluded from this adoption batch. No telemetry activation, ServiceMonitor, metrics identity or VSO token delivery is introduced by #65. The complete metrics-only listener and enforcement are owned by follow-up Infra PR #71; no intermediate token design needs deployment.
- Shared charts adopted: openbao-oidc (Radar/ABS/Longhorn), postgres-consumer (Radar/Grafana), workload-network (Radar; existing ABS native network interface retained), service-route (OpenBao/Longhorn proxy).
- Private configuration remains Live. No upstream chart versions, workload replicas, admin groups, storage data, or credential identities are changed.

The stage descriptions below record the implementation plan and its gates; this current-status section supersedes earlier pending decisions. Runtime scrape success, login renewal/max-TTL recovery, namespace field ownership and resource-owner handoffs are not proven by offline tests. Publishing draft branches does not authorize merge, sync, prune or deployment.

### Native platform alert migration status and handoff gate

The native alert identity migration is deferred. Live inspection during the #72 handoff found the existing `kube-prometheus-stack/platform-required` rule still present, with no native replacement. Retain `09-kube-prometheus-stack/resources/alerts.yaml` with its complete original spec and omit `additionalPrometheusRulesMap.platform-required`. This keeps one desired and selected copy without deleting any live resource. `tests/test_native_alert_rules.py` compares the standalone resource against the immutable fixture and renders pinned chart 92.10.1 to reject a duplicate `platform.required` group. Existing PDBs and discovery settings remain unchanged.

**Any future native alert identity migration remains gated on an explicit, separately approved controlled handoff.** The proposed new identity is `kube-prometheus-stack/kube-prometheus-stack-platform-required`; the old identity is `kube-prometheus-stack/platform-required`. Automated pruning is false: removing the old file does not delete the old live PrometheusRule. With `ruleSelectorNilUsesHelmValues: false`, the rendered `ruleSelector: {}` and `ruleNamespaceSelector: {}` select both copies, including the unlabeled old rule. Syncing this preparation alone can therefore evaluate duplicate alerts; matching release labels do not prevent this.

Before deployment, inspect non-secret ownership and current rule loading, confirm exact spec equality, and obtain approval for an attended sequence that coordinates new-rule activation with explicit old-rule retirement. Verify that Prometheus loads exactly one copy of `platform.required` with all six alerts, with no rule evaluation errors; do not leave both selected resources indefinitely or assume Alertmanager deduplication makes coexistence safe. No atomic interruption-free handoff has been demonstrated by offline rendering: if an acceptable nonduplicating transition cannot be established, keep the standalone rule active and defer/revert this desired-state migration before syncing. Rollback must likewise coordinate restoration of the old resource with separately approved retirement of the new identity; a Git revert alone does not remove it. No live inspection, deletion, sync, deployment, push, or other live operation was performed or authorized here.

## Native monitoring and object migration implementation

The approved repository-only batch enables native cert-manager/K8up/Longhorn ServiceMonitors, the CNPG operator PodMonitor with `release: kube-prometheus-stack`, and Argo controller/server/repoServer/applicationSet/notifications metrics and ServiceMonitors. Redis/Dex additions remain disabled. K8up now uses `k8up.enableLeaderElection`; the ignored root key is removed. Longhorn adds only the TCP/9500 scrape peer requiring monitoring namespace AND both Prometheus pod labels; existing internal policies remain unchanged. The pinned templates/values in `/tmp/platform-chart-audit` provide these supported mechanisms; no custom generator is needed.

Live's existing Argo Application patch now enables the native `argocd-server` HTTPRoute with the old host, Gateway, explicit defaults and HTTPS Service backend port 443. The old desired `infra/argocd-config/route.yaml` is retired; BackendTLSPolicy remains. **The old live `argocd/argocd` HTTPRoute is NOT automatically pruned** (`prune: false`). It remains until an attended, separately approved retirement after the new route has Accepted/ResolvedRefs and verified TLS/routing. Do not infer deletion from the removed file.

SMB now uses a registered Live Application patch with native `storageClasses`. A pre-retirement exact render comparison proved the same `smb` identity and full spec, including all parameters, ordered mount options and immutable fields. Only normal chart metadata labels are added. The standalone Live file and its now-empty Application source are retired; the public resource source containing the existing Namespace remains. No StorageClass replacement/deletion, storage migration, or data changes are required or authorized.

`tests/test_native_chart_migrations.py` builds the current Live Application overlay against a local public base and renders exact pinned charts offline. It checks native monitor discovery/ports, Argo monitoring API gating and route equivalence, SMB full-spec equivalence, and preservation of Longhorn internal policies. It does not contact a cluster or print rendered Secrets. Namespace migration, replicas, and PDBs are outside that monitoring-object batch. The separately approved native platform alert preparation is recorded in the status section above.

Before rollout: publish/review the coordinated Infra and Live revisions together, inspect resource tracking/SSA ownership (especially existing `smb`), confirm monitoring CRDs/discovery and scrape connectivity, verify new Gateway conditions/backend TLS, and approve any old-route retirement separately. No sync, push, commit, prune or deployment was performed by this batch.

Basis: [version-specific capability audit](chart-native-capabilities-audit.md). All paths below are relative to the named repository. Existing uncommitted Apps/Live Radar changes are the starting point, not disposable changes.

## Objectives and boundaries

1. Prefer supported chart values or operator fields over our additional manifests.
2. Use Argo CD Application metadata for its destination namespace, with one metadata steward per namespace.
3. Keep private configuration in Live. Follow the direct public Infra root and native external Helm value-file direction in Bootstrap issue #62; retain Apps external `valueFiles`. The old private Kustomize root is only a staged rollback path.
4. Add small shared Helm components only for demonstrated repeated integrations. Keep concrete one-off resources in YAML/Kustomize.
5. Preserve application/resource names, permissions, storage, rotation, sync waves, and Secret references unless a specific change is stated below.
6. No chart upgrades, replica increases, shared privileged credentials, resource deletion, pruning, or live deployment as part of repository cleanup.

Stages 1 and 2 are the first implementation batch. Stage 3 adds explicit availability behavior. Stage 4 is a separately tested ownership/identity migration. Stages 5 and 6 require their stated design/render gates; a capability being available is not authorization to enable it everywhere.

## Stage 0 — Baseline and tests

Before changing configuration:

- Record the exact current chart versions, input values, and rendered non-secret resource identities. Do not copy Secret payloads into reports or committed fixtures.
- Render public Applications together with their current Live patches; render each chart and each supporting-resource source independently, then compare effective Argo source precedence.
- Capture Namespace labels, PDB selectors/budgets, NetworkPolicy rules, routes/backends, StorageClass specifications, RBAC permissions, Secret references, and sync waves.
- Add focused regression tests before each migration. Use local copies of remote Kustomize bases for offline tests, not an assumed already-pushed Git revision.
- Keep resource tracking and namespace SSA adoption checks as a later, read-only live gate. Repository state is insufficient proof of deployed ownership.

## Stage 1 — Move destination-namespace metadata to Applications

For the following eleven Applications, set `spec.syncPolicy.managedNamespaceMetadata.labels` to the labels below. Retain their existing `CreateNamespace=true`. Remove only the matching Namespace YAML document after testing. If a resource source becomes empty, remove that source too; retain any other resources in the same file/directory.

Label abbreviations below expand to exact Kubernetes label keys:

- `enforce=restricted/privileged`: `pod-security.kubernetes.io/enforce`.
- `warn=restricted` and `audit=restricted`: corresponding Pod Security labels.
- `ambient`: `istio.io/dataplane-mode: ambient`.

| Application file in Infra | Namespace | Exact labels to preserve | Namespace resource to retire |
| --- | --- | --- | --- |
| `00-letsencrypt-dns01-issuer/app.yml` | istio-gateway | enforce=restricted | `00-letsencrypt-dns01-issuer/resources/namespace.yaml` |
| `01-bootstrap/istio/app.yml` | istio-system | enforce=privileged | `01-bootstrap/istio/base-resources/namespace.yaml` |
| `02-controllers/cert-manager/app.yml` | cert-manager | enforce=restricted, ambient | `02-controllers/cert-manager/resources/namespace.yaml` |
| `02-controllers/cloudnative-pg/app.yml` | cloudnative-pg | enforce=restricted, ambient | Namespace document only in `resources/availability.yaml` |
| `02-controllers/crossplane/app.yml` | crossplane-system | enforce=restricted, ambient | `02-controllers/crossplane/resources/namespace.yaml` |
| `02-controllers/k8up/app.yml` | k8up | enforce=restricted, ambient | Namespace document only in `resources/availability.yaml` |
| `02-controllers/longhorn/app.yml` | longhorn | enforce=privileged, warn=restricted, audit=restricted | `02-controllers/longhorn/resources/namespace.yaml` |
| `02-controllers/vault-secrets-operator/app.yml` | vault-secrets-operator | enforce=restricted, ambient | `02-controllers/vault-secrets-operator/resources/namespace.yaml` |
| `04-openbao/app.yml` | openbao | enforce=restricted, ambient | Namespace document only in `resources/security.yaml` |
| `08-backup-validation/app.yml` | backup-validation | enforce=restricted, ambient | Namespace document only in `resources/validation.yaml` |
| `09-kube-prometheus-stack/app.yml` | kube-prometheus-stack | enforce=restricted, ambient | Namespace document only in `resources/grafana.yaml` |

Example of the intended shape:

```yaml
syncPolicy:
  managedNamespaceMetadata:
    labels:
      istio.io/dataplane-mode: ambient
      pod-security.kubernetes.io/enforce: restricted
  syncOptions:
    - CreateNamespace=true
    - ServerSideApply=true
```

Preserve all other existing sync options and automation settings; this abbreviated example is not a replacement of the whole syncPolicy.

### Explicit exceptions

- Radar and Audiobookshelf already use Application namespace metadata: leave them unchanged.
- Keep `node-exporter` as a standalone Namespace, including privileged/warn/audit labels and sync wave -20. It is not monitoring's destination namespace.
- Leave `kube-system` ownership unchanged in this batch. A storage driver should not silently become the metadata owner of the entire shared system namespace.
- Include `argocd` metadata adoption through `argocd-config` after a live ownership preflight, with CreateNamespace and the existing restricted/warn/audit labels. The current Namespace file is not sourced, so this is new metadata management, not just relocation. Bootstrap deliberately creates a bare Namespace first, validates restricted-compatible chart workloads, then installs them; do not enable restricted admission before that bootstrap preflight.
- Do not duplicate namespace metadata into every Application sharing `cloudnative-pg`, `crossplane-system`, `istio-system`, or monitoring namespaces.
- Do not add manual Argo tracking annotations or delete/prune Namespace resources. Before sync, determine whether existing CSA/SSA/tracking ownership requires an attended adoption step.

Acceptance: exact authored labels preserved; eleven intended namespace definitions moved; other documents retained; no empty resource paths; no destination changes; early namespace creation order retained. Update Infra tests that currently require standalone Namespace objects.

## Stage 2 — Remove incorrect or duplicate configuration

### 2.1 OpenBao PDB

File: Infra `04-openbao/app.yml`, first chart source, `helm.valuesObject`:

```yaml
server:
  ha:
    disruptionBudget:
      enabled: true
      maxUnavailable: 1
```

Merge into the existing server/HA settings. Remove only the authored PDB document from `04-openbao/resources/security.yaml`.

Expected identity remains `openbao/openbao`. Explicitly accept the native selector becoming server-specific: name + instance + `component: server`. Verify current pod-template labels in the rendered chart and later in live metadata. This avoids selecting injector/other OpenBao-labeled workloads unintentionally. Do not touch Raft replica count or policies.

### 2.2 K8up leader election

File: Infra `02-controllers/k8up/app.yml`, chart `helm.valuesObject`:

```yaml
k8up:
  enableLeaderElection: true
```

Remove unused root `leaderElection`. Preserve all existing nested `k8up` values. Two replicas remain unchanged. The effective default was already true, so this is configuration correction, not a new HA rollout.

Acceptance: one desired OpenBao PDB identity, fixed maxUnavailable 1, expected selector; K8up deployment still receives the leader-election option. Keep cert-manager/Grafana existing PDBs unchanged.

## Stage 3 — Add three native PDBs deliberately

This is an availability-policy addition, not pure relocation. No replica increases are proposed.

### 3.1 Longhorn UI

Infra `02-controllers/longhorn/app.yml`:

```yaml
longhornUI:
  podDisruptionBudget:
    enabled: true
    minAvailable: 1
```

Existing two UI replicas stay unchanged. Do not enable the global-manager budget or change CSI/instance-manager budgets as a side effect.

### 3.2 Prometheus and Alertmanager

Infra `09-kube-prometheus-stack/app.yml`:

```yaml
prometheus:
  podDisruptionBudget:
    enabled: true
    minAvailable: 1
    unhealthyPodEvictionPolicy: IfHealthyBudget
alertmanager:
  podDisruptionBudget:
    enabled: true
    minAvailable: 1
    unhealthyPodEvictionPolicy: IfHealthyBudget
```

Both currently have two replicas. `IfHealthyBudget` is explicit and conservative rather than silently adopting the chart's `AlwaysAllow` default. It may delay a drain when availability is already degraded; this is a conscious trade-off. Verify the target Kubernetes API supports this field before live adoption.

### 3.3 Do not add/change

- Existing cert-manager and Grafana budgets: retain current values.
- Barman, CloudNativePG operator and K8up: retain standalone minAvailable 1 PDBs; no native chart feature exists.
- Radar, Audiobookshelf, VSO, Crossplane, Istiod: no new singleton-blocking budgets or automatic replica changes.
- DaemonSets/CRD-only charts: no blanket PDB rule.

Acceptance: exactly three new PDB identities; selectors match their intended workloads; only one min/max field; ready replicas sufficient before sync. PDBs do not guarantee application-level failover or upgrade availability.

## Stage 4 — Replace concrete standalone objects with native values

Perform one migration at a time. Keep identities if supported; otherwise document coexistence and attended old-object retirement. With prune disabled, removing a file does not remove a differently named live resource.

### 4.1 SMB StorageClass

Files:

- Add Live `infra/applications/patches/csi-driver-smb.yaml` and register it in the existing Infra Application overlay.
- Add `storageClasses` under the SMB chart's `helm.valuesObject` using the exact content of Live `infra/csi-driver-smb/storageclass.yaml`.
- Retire the standalone file/source only after comparing rendered spec and identity.

Exact configuration to preserve:

- name `smb`; provisioner `smb.csi.k8s.io`;
- `reclaimPolicy: Retain`, `volumeBindingMode: Immediate`, `allowVolumeExpansion: true`;
- existing private share path, `subDir`, `onDelete: retain`, both CSI Secret references;
- every mount option, unchanged and in the existing order.

Private paths stay in Live. No StorageClass deletion/recreation, PV/PVC migration, or data copy. If rendered immutable fields differ, stop rather than forcing replacement. Chart-added metadata labels must be reviewed but are not a reason to change the storage spec.

### 4.2 Argo CD HTTPRoute

Add chart-native values in the existing Live `infra/applications/patches/argocd-config.yaml`:

```yaml
server:
  httproute:
    enabled: true
    parentRefs:
      - group: gateway.networking.k8s.io
        kind: Gateway
        name: istio-gateway
        namespace: istio-gateway
        sectionName: https
    hostnames: ["<existing private Argo hostname>"]
    rules:
      - matches:
          - path:
              type: PathPrefix
              value: /
```

The existing public Application chart values remain in place. The new native route's name is `argocd-server`, not the old `argocd`. Backend must remain Service `argocd-server:443`, with TLS configuration unchanged. Preserve BackendTLSPolicy and backend trust resources.

Gate: verify all explicit parent/backend default fields, HTTPS backend selection, and Gateway Accepted/ResolvedRefs. Keep the old route until the new route is verified. No automatic `kubectl delete`; any old-route retirement needs a separately approved operation. Do not rename the Argo Service/fullname just to preserve the route name.

### 4.3 Platform Prometheus rules

In Infra `09-kube-prometheus-stack/app.yml`, add `additionalPrometheusRulesMap.platform-required.groups` copied exactly from `resources/alerts.yaml`.

Preserve all six alert names, expressions, durations, severity labels, and group name. Do not redesign thresholds. Verify chart-generated labels match Prometheus rule discovery.

The chart prefixes the resource name. Plan old/new identity transition: do not leave two selected copies indefinitely and generate duplicate alerts. If an interruption-free nonduplicating handoff cannot be established, retain the current standalone PrometheusRule; this is an organizational cleanup, not a requirement to change a working alert definition.

### 4.4 OpenBao route defaults

The pinned chart cannot supply missing backend group/weight through native values. The implemented shared `service-route` component now owns the same OpenBao route identity and complete prior spec, adding only explicit backend group/weight. Native OpenBao route generation is disabled. Exact-spec tests cover this transition; no upstream chart upgrade is involved.

## Stage 5 — Network and monitoring adoption gates

Do not set every available `networkPolicy.enabled` or monitor flag to true in one batch. These change connectivity and observability, and need per-consumer proofs.

### 5.1 OpenBao server NetworkPolicy migration

Approved after reviewing the selector distinction: the native policy is now enabled with exact previous ingress/egress arrays. It adds `app.kubernetes.io/instance: openbao` to the old name-only selector; tests prove declared server pods carry both labels. This is equivalence for the intended workload, not a claim that selector populations are identical.

The policy identity changes from `allow-openbao-raft-and-clients` to `openbao`. The old live policy will remain under prune=false and requires a separately approved retirement after target-pod/connectivity verification. Keeping it indefinitely would retain its broader target selection. Namespace-wide default deny and Ambient/HBONE allowances remain unchanged. The subsequent [API ownership candidate](api-network-ownership.md) removes only the native server's broad TCP 6443 rule, moves injector API tuples to Live, and preserves snapshot HTTPS as a separate broad Live exception. These additive allowances are not R2-only enforcement. Native default rule lists were not adopted.

### 5.2 Monitoring configuration targets

These are exact candidate switches, not an instruction to enable them without their gate:

| Consumer | Native setting | Required gate |
| --- | --- | --- |
| cert-manager | `prometheus.servicemonitor.enabled: true` | Preserve existing metrics settings; do not also enable PodMonitor; verify scrape labels and connectivity. |
| CloudNativePG operator | `monitoring.podMonitorEnabled: true`, `monitoring.podMonitorAdditionalLabels.release: kube-prometheus-stack` | Current Prometheus PodMonitor selector requires exactly that release label; do not change global discovery or conflate with database-instance monitoring. |
| K8up | `metrics.serviceMonitor.enabled: true` | Verify endpoint and selector; rule/dashboard features are separate opt-ins. |
| Longhorn | `metrics.serviceMonitor.enabled: true` and constrained `networkPolicies.metricsScrapeSources` | Restrict to the intended monitoring namespace/workload, retain internal isolation. |
| VSO | `telemetry.serviceMonitor.enabled: true` | Approved and enabled in repository configuration: native HTTPS scrape with Prometheus SA token. VSO 1.6.0 hardcodes `tlsConfig.insecureSkipVerify: true`; this is explicitly accepted with Ambient mTLS between monitoring and VSO. No custom proxy, certificate plumbing, ignored TLS values or auth bypass. Live mesh enrollment and scrape success remain rollout checks. |
| Argo CD | Selected component `metrics.enabled` and `metrics.serviceMonitor.enabled` | Choose actual enabled components; verify namespace policy and telemetry endpoints. |
| OpenBao | Deferred to Infra PR #71 | This adoption batch leaves OpenBao metrics disabled. The follow-up owns the complete native metrics-only listener, ServiceMonitor and port/identity enforcement, without bearer-token machinery. |

Targeted rendering confirmed ServiceMonitor discovery accepts all labels/all namespaces, while PodMonitor discovery requires `release: kube-prometheus-stack`. No extra release label is needed for the proposed ServiceMonitors. Do not relax global PodMonitor discovery.

cert-manager scrapes HTTP 9402, CNPG operator HTTP 8080, K8up HTTP 8080. No selecting authored network policy blocks those endpoints. CNPG and Prometheus both use Ambient, satisfying the repository's STRICT mesh setup; live transport must still be tested. Longhorn uses HTTP 9500 and requires the explicit namespace AND Prometheus-pod scrape peer, because its internal manager policy is enabled despite `networkPolicies.enabled: false`.

VSO implementation check against the official v1.6.0 source confirmed the limitation in [`chart/templates/prometheus-servicemonitor.yaml`](https://github.com/hashicorp/vault-secrets-operator/blob/v1.6.0/chart/templates/prometheus-servicemonitor.yaml#L36-L37) and [`chart/templates/deployment.yaml`](https://github.com/hashicorp/vault-secrets-operator/blob/v1.6.0/chart/templates/deployment.yaml#L49-L65). Merely supplying a CA/serverName in values will not affect either template. A verified-TLS implementation would require both a managed serving certificate wired into the proxy and a monitor that consumes its trust configuration. After explicit user approval, the native ServiceMonitor was enabled through chart values, retaining token authentication and accepting the chart TLS behavior with Ambient mTLS. No cluster sync was performed.

Argo metrics can be enabled for controller, server, repoServer, ApplicationSet, notifications and enabled Dex through each component's two metrics switches. Initially select controller/server/repoServer/ApplicationSet/notifications only; leave Dex and Redis additions out unless their metrics are required. Redis also needs `redis.exporter.enabled: true`, a new sidecar, not just a monitor flag. Monitor templates requiring the monitoring API must render with the actual API capability, including offline tests.

OpenBao telemetry, metrics listener ports, scrape configuration and their network/identity enforcement are introduced together by Infra PR #71, not by this adoption batch. The obsolete Policy/AuthBackendRole/ServiceAccount/VSO metrics-token path is not published here. Review and accept the follow-up's policy-before-listener rollout independently; normal API/Raft and other monitoring consumers remain unchanged.

Additional dashboards/rules are separate opt-ins; validate their queries first.

Other native network policy options (Istio, cert-manager, monitoring stack) remain available but unchanged until their traffic matrices are verified. No chart-default policy is assumed equivalent to namespace-wide default deny or to the current K3s/Ambient setup.

## Stage 6 — Shared components for what remains

Public implementations belong under Infra `charts/`; private consumer settings remain in existing Live values/overlays. Build one component at a time. Start with render-equivalence tests for the existing consumers before replacing their resources. Preserve names to avoid reissuing OIDC clients or credentials.

### 6.1 First: `charts/openbao-oidc`

Consumers: Radar, Audiobookshelf, Longhorn. It should create only the OIDC integration and, when explicitly owned by this component, narrowly scoped Secret delivery. Inputs include:

- explicit existing resource/provider/key/client names and namespace;
- HTTPS issuer, exact callback URI list, TTLs;
- assignment/group references, scope definitions and existing shared scope references;
- providerConfig reference, connection-Secret name and VSO output-Secret name;
- explicit deletion policy and ordering; no insecure defaults or default administrator assignment.

Preserve distinct authorization:

- Radar: existing username/groups claims, `allow_all` authentication and separate Kubernetes admin-group authorization.
- Audiobookshelf: approved group assignment, app-specific admin scope and shared profile reference; API configurator remains app-specific.
- Longhorn: administrator assignment plus its separate oauth2-proxy. Preserve proxy cookie Secret and routing. Do not turn it into native Longhorn authentication.

Prefer consuming existing VSO auth/connection resources; do not create a second competing auth identity. Migration must have one resource owner at a time. Do not recreate generated clients, alter sensitive material, or print generated Secret values during tests.

A standalone shared chart is an explicit Argo source. Audiobookshelf could instead consume it as a pinned chart dependency if that demonstrably simplifies local/offline packaging; decide once, rather than maintaining two equivalent implementations. Do not introduce packaging or a publishing pipeline until the existing Git-chart mechanism fails a demonstrated need.

### 6.2 Second: `charts/postgres-consumer`

Consumers: Radar and Grafana. Scope: existing CNPG Database + OpenBao SecretBackendStaticRole + consumer-specific VSO delivery, with explicit auth references. Preserve resource names, rotation configuration, grants and sync waves.

Outputs differ: Radar DSN versus Grafana username/password. CNPG-managed manager identity stays separate from OpenBao-managed rotating consumer identities. This is not a chart for the PostgreSQL server/operator itself.

### 6.3 Network and route reuse

Concrete proposed components, not separate charts for every port:

- **Single-Service HTTPRoute:** two real consumers are Longhorn's route to `longhorn-oidc-proxy:80` and OpenBao's route to `openbao-active:8200` (the latter needs explicit default fields unsupported by its pinned chart). Preserve each route's namespace/name, hostnames, Gateway parent, filters/timeouts, and Service/port/weight. OpenBao native generation must be disabled in the same desired configuration. Argo, Radar, Grafana and Audiobookshelf keep suitable native route support rather than becoming consumers merely for uniformity.
- **DNS egress:** OpenBao server/injector/snapshot, PostgreSQL, backup validation, Radar, Audiobookshelf and provisioning/configuration jobs. Parameterize source selector; preserve the current kube-system namespace peer with UDP/TCP 53. Do not silently tighten to a CoreDNS pod selector or include unrelated API ports.
- **OpenBao client egress:** Longhorn/PostgreSQL/Radar credential jobs and Audiobookshelf configurator share the destination `namespace=openbao` AND `pod name=openbao`, TCP 8200/15008. Other consumers' broader namespace peers or differing ports are explicit exceptions, not silently normalized.
- **Gateway ingress:** Radar port 9280 and Audiobookshelf port 13378, with HBONE 15008. Preserve their different target selectors and Audiobookshelf's separate configurator ingress.
- **Default deny:** accept an explicit selector and policyTypes; namespace-wide OpenBao/Radar/backup-validation isolation must not be confused with PostgreSQL/ABS workload-only isolation.

Package demonstrated network patterns together only if a small values interface actually reduces duplication; do not introduce five new Argo sources for five rules. Native upstream route/policy support stays preferred; never generate two owners for the same object. Keep OpenBao-specific Raft/injector/snapshot exceptions explicit. Render policy peers with namespace AND pod selectors in the same peer where both are intended.

Keep the three standalone operator PDBs as short manifests initially. A dedicated PDB chart/source is not justified merely to remove three small files.

### 6.4 VSO sharing before a generic secret chart

First prove native namespace-qualified connection references and SecretTransformation reuse with existing VSO 1.6.0 APIs. Retain isolated ServiceAccounts, policies, auth roles, secret paths, token audiences and restart behavior. Do not turn on defaultAuthMethod or a broad VaultAuthGlobal in this batch.

Only introduce another component if meaningful duplicated wiring remains after native reuse; the OIDC/database charts may already cover the demonstrated need.

## Explicitly unchanged

- Chart versions, protected main tracking, replica counts, automated prune settings and Audiobookshelf disabled automated sync.
- Longhorn proxy route/backend, its three encrypted StorageClasses and encryption-Secret references.
- PostgreSQL/OpenBao backup retention, checksums, restore validation and specialized adapters/jobs.
- Local-media admission policy, existing static PVs, storage data and app migration authorization.
- Platform-owned OIDC profile scope, human-group membership, administrator permissions and application identity boundaries.
- Public Infra inline defaults and native external private Live values; Apps external Live values convention.

## Validation and approval boundaries

### Repository checks

- Parse authored YAML; run existing Infra and Apps contract suites, updating assertions for intended ownership changes only.
- Run Helm renders for every affected exact chart version, including Live overlays; compare expected resource identities and selected behavioral fields.
- Assert no duplicate desired resource identities across combined sources, except explicitly documented temporary handoffs; no dropped resources.
- Assert exact namespace labels, least-privilege rule equality, PDB selectors, Secret references, retention settings and all HTTPRoute default fields.
- Test missing required OIDC values/unsafe configuration as failures when shared components are introduced.
- `git diff --check` in all touched repositories; do not stage/commit unrelated existing changes.

### Before any live change

Provide exact proposed commands, target Applications/resources, expected diff, ordering, namespace ownership/adoption checks and rollback steps. Obtain fresh approval for each mutating command. No automatic commit/push, sync, prune, namespace deletion, StorageClass replacement, secret rotation or restart is included in this plan.

Publish shared chart/base additions before consumer references, and Live dependencies before enabling corresponding public Application sources where needed. Local unpushed renders alone do not prove a remote reference exists. Avoid partial cross-repository states by reviewing the release sequence per batch.

Rollback is per migration: restore old desired resources/values and disable new generation without deleting namespaces/storage. Differently named new objects may require separately approved retirement, and rollback must not restore duplicate alerts or overlapping broad policies indefinitely. Keep the existing route/security resource active until its replacement is verified; do not treat a Git revert as proof that live ownership has been restored.
