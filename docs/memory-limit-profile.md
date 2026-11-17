# Measured native RAM limits — not scheduler savings

This is a RAM-limit-only follow-up to [request sizing](request-rightsizing.md).
Source CPU/RAM requests, CPU caps (including their absence), chart versions,
replicas, placement, HA, storage membership, networking and credentials stay
unchanged. No VPA, admission extension, namespace default or Pod patch.

## Caps and evidence

Read-only2026-11-17T14:52:27Z inventory:128 long-lived +11 ordinary init containers.
65/128 long-lived containers had RAM caps;63 did not. Both Prometheus replicas
were queried individually at one epoch over7d, with conservative maxima across
replicas, not summed duplicates. The table uses cAdvisor's reported charged
memory high-water, not request P95, process RSS or working set alone. It includes
page cache and can retain a peak from before the7d window in an old cgroup.
These observations are not complete cold-start/failover/load bounds.

| Native resources path | Old RAM cap | New cap | Observed high-water MiB |
|---|---:|---:|---:|
| Argo applicationSet.resources |none|256Mi|127.6|
| Argo dex.resources (SERVER) |none|256Mi|112.6|
| Argo notifications.resources |none|256Mi|131.3|
| Argo redis.resources |none|128Mi|41.8|
| cert-manager cainjector.resources |none|256Mi|118.7|
| cert-manager webhook.resources |none|256Mi|75.3|
| OpenBao injector.resources |none|128Mi|59.9|
| prometheusOperator.resources |none|256Mi|101.2|
| kube-state-metrics.resources |none|384Mi|172.3|
| grafana.sidecar.resources, BOTH sidecars |none|256Mi each|136.9 /120.5|
| prometheusOperator.prometheusConfigReloader.resources |none|128Mi|42.8 steady|
| prometheus.prometheusSpec.initContainers[name=init-config-reloader].resources |none|512Mi|151.4 isolated synthetic charged peak|
| ztunnel chart resources |1Gi|256Mi|58.8|
| Crossplane resourcesRBACManager, main AND init |512Mi|128Mi|54.2 main /53.9 init|

All are native fields in the existing six Applications. Helm retains the existing
Crossplane RBAC100m CPU cap from chart defaults; main Crossplane remains512Mi,
not128Mi. The two Grafana sidecars intentionally share one resources profile.

### Reloader: coupled steady/init limits are mandatory

Operator0.94.1's global reloader resources also feed generated INIT. Setting only
128Mi would leave the named init request256Mi above its inherited cap and fail
API admission. Keep the named request100m/256Mi and override its memory cap to
512Mi. The operator's named merge must preserve generated image/args/mounts.

The shared flag also applies to future operator-managed reloader consumers,
including Alertmanager (currently replicas0, unchanged). Enabling another CR
requires sizing its config/init independently; this is not an unlimited future
payload guarantee. The exact Thanos0.42.4 synthetic startup replay covered up to
32MiB configs, charged peak151.4Mi /child RSS141.7Mi. Production config contents/
size were deliberately not read; replay was not a cold-cache production test.

## Explicit exclusions

- Barman INSTANCE sidecar retains250m/192Mi requests and250m/256Mi caps. Pi had
  a recorded OOM2026-11-12 at this cap: it requires separate cause/backup/restore
  investigation, not a guessed safe512Mi cap here. A capped peak is censored.
- PostgreSQL1Gi, Prometheus2Gi, Grafana512Mi, main Crossplane512Mi, OpenBao server
  512Mi and existing controller/plugin caps remain intact. Several reach their
  cap; low RSS or idle CPU is not permission to reduce them.
- Longhorn CSI plugin reported3058.6Mi high-water on OMV,518.2Mi Wyse,471.5Mi Pi;
  a blanket128/256/512Mi cap would be unsafe. No storage/CSI/IM/node-plugin change.
  The encrypted-discard helper keeps2Gi (observed high-water1051.4Mi).
- Dex copyutil stays separate and uncapped: its16Mi REQUEST is NOT a safe cap.
  Exact-image copying charged~247Mi including dirty cache despite~2.5Mi RSS.
  Repo copyutil keeps512Mi. No tiny init limit from sparse scrapes.
- ServiceLB admission defaults, K3s-owned CoreDNS/Metrics Server and private Apps
  are untouched. Unsupported Longhorn component fields are not invented.
- No new CPU caps: PostgreSQL/Barman and other existing caps already throttle.
  Throttled CFS-period percentages do not measure lost wall time or establish
  performance causality. No CPU-cap A/B experiment is performed here.

## Regression / acceptance boundaries

Actual exact-chart regressions resolve workload AND container, including shared
RBAC init and both Grafana sidecars. Restoring only old RAM caps and normalizing
only those allowlisted rendered memory-limit fields must leave every other
rendered document equal, including requests, CPU caps, security and HA.
The typo negative control proves an ignored resources key does not count.
Existing request regressions now preserve independent caps; Dex init selection
uses zero requests in the render-only baseline instead of falling back to the
newly capped Dex server. Historical request counterfactuals are NOT admitted Pods.

The existing owned network-isolated K3s API job also verifies explicit requests
are not defaulted up to limits, real Kubernetes strategic merge preserves init
image/args, paired override is admitted, and inherited128Mi without init512Mi is
rejected. It has no agents or image execution. This is native merge/API proof,
NOT execution of Prometheus Operator or actual production startup.

Before merge/activation, separately accept normal GitOps lifecycle: startup/
reconcile, mesh reconnect/load, Argo sync, Crossplane RBAC init and generated
Prometheus steady/init. Check all-node fit, pending/surge/failover and physical
HOST plus POD-PARENT cgroup headroom, not merely individual working sets. Caps
can establish a Pod-parent ceiling and change runtime GC behavior. Existing
pending source requests may be adopted during the same rollout even though this
source diff changes none. No production functional/load/backup tests or private
integration are claimed by public CI. Draft is not a technical activation lock.

**Scheduler reservation savings: ZERO.** The current native Pi projection stays
2108Mi, full IM/ServiceLB profile2568Mi versus2337.664Mi observed allocatable;
the existing230.336Mi deficit is not fixed. Independent maxima are not measured
simultaneous demand; sum of caps can exceed physical RAM without guaranteeing it.

## Rollback / ownership

Main auto-reconciles: merging can roll these controllers and mesh Pods. PR
creation is not rollout approval. No manual restart or sync is part of this work.
If an approved rollout regresses, revert the limit-only commit through the same
GitOps owner: restore old1Gi ztunnel /512Mi RBAC caps and remove only newly added
RAM-cap fields, preserving requests, CPU settings and named init request. Review
normal reconciliation/health before any further action; reverting may roll Pods
again. Do not delete storage/DB Pods or weaken protected memory reserves.
