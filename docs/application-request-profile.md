# Native application CPU / memory requests

This resource-only profile fills supported application fields without changing
limits, replicas, affinity, priority, storage membership, credentials or network
permissions. Longhorn CSI/global-manager and Barman resource profiles already
exist and are not duplicated here. CoreDNS/Metrics Server remain K3s-owned.

## Per-container profile

| Application/component | CPU | RAM | Native resource path in valuesObject |
|---|---:|---:|---|
| Argo ApplicationSet | 10m | 96Mi | applicationSet.resources |
| Argo Dex | 10m | 144Mi | dex.resources |
| Dex copyutil init | 100m | 16Mi | dex.initImage.resources |
| Argo Notifications | 10m | 144Mi | notifications.resources |
| Argo Redis | 10m | 48Mi | redis.resources |
| cert-manager cainjector, per replica | 10m | 144Mi | cainjector.resources |
| cert-manager webhook, per replica | 10m | 96Mi | webhook.resources |
| OpenBao agent injector (not servers) | 10m | 80Mi | injector.resources |
| Prometheus Operator | 10m | 128Mi | prometheusOperator.resources |
| kube-state-metrics | 10m | 224Mi | kube-state-metrics.resources |
| Grafana dashboard/datasource sidecars, each | 10m | 128Mi | grafana.sidecar.resources (shared) |
| Running Prometheus config-reloader | 10m | 48Mi | prometheusOperator.prometheusConfigReloader.resources |
| Prometheus init-config-reloader | 100m | 256Mi | prometheus.prometheusSpec.initContainers, named resource-only override |

Regular-container candidates use available seven-day per-series working-set P95
plus approximately 20%, rounded, and a useful 10m CPU floor above measured CPU
P95. This is not uninterrupted seven-day steady-state/recovery evidence. Requests
are reservations, not hard caps; all existing limits/no-limit semantics remain.

## Startup evidence / native override

[Official isolated measurement](https://github.com/dmuiX/k3s-on-omv-infra/actions/runs/44072002284)
at `6fc19938489909e87097c4e8e4b00a864dd5b07c`: six copyutil starts, sixteen
synthetic reloader starts and two controls. All outputs verified; zero OOM or
timeouts; all owned containers removed. Exact public production amd64 image
digests, non-root identities, no network and no production configuration read.

- Copyutil: maximum child RSS 2.480MiB, sampled working set 8.188MiB, charged
  cgroup peak 246.688MiB including 238.598MiB sampled dirty file cache. The 16Mi
  REQUEST is not a tiny hard LIMIT: allow separately budgeted dirty-write/cache
  startup burst and inspect ancestor cgroup headroom. No cold-cache worst-case
  or instant reclaimability claim.
- Reloader child RSS maxima: 34.625MiB at 1MiB synthetic input, 60.250MiB at 8MiB,
  141.742MiB at 32MiB. The 256Mi init request has margin for the tested envelope,
  not a proof for unknown production payload sizes. CPU work was <=267ms; 100m
  is a modest startup reservation, not a fixed-latency guarantee under contention.
- [Operator v0.94.1 merges named init overrides](https://github.com/prometheus-operator/prometheus-operator/blob/v0.94.1/pkg/prometheus/server/statefulset.go#L307-L320).
  Only resources are supplied: generated images/arguments/volumes are preserved.
  The running reloader stays 10m/48Mi; the existing 250m/1Gi main Prometheus
  container dominates this init's scheduler maximum. Dex's 100m init can increase
  its CPU reservation, so account for actual replicas rather than containers only.

## Source checks and activation boundary

Exact pinned Helm renders check every target by workload AND container name,
separate init/default reloader fields, a misspelled-key negative control and
whole-resource equality after stripping ONLY the intended request changes.
Private Live/Apps/Bootstrap integration and actual operator adoption are separate.

Merging into main can trigger automatic Argo reconciliation and Pod replacement;
a PR is not permission to activate it. Before merge/activation: fresh all-node
scheduler and physical/ancestor-cgroup budgets, eligible placement, rollout surge
and failover (including all Pending Pods), storage/backup/API health, and explicit
approval. Preserve HA throughout; observed placement is not a placement guarantee.
The existing paired Pi reserve gate is unchanged; this profile does not make the
full unreserved Longhorn/ServiceLB planning target fit that node.

Rollback is a reviewed revert of these request fields, followed by normal GitOps
reconciliation and health checks; do not manually patch operator-owned Pods or
restart databases/storage. Reverting requests is another Pod lifecycle change,
not an immediate running-Pod rollback. No source CI result is rollout acceptance.

Longhorn V1 instance-manager RAM, engine-image/UI/deployer/helpers and K3s
ServiceLB have no component-specific override in the installed versions. No
LimitRange or admission mutation is introduced by this profile. A separately
reviewed admission proposal for instance-manager RAM / ServiceLB must carry its
own capacity and lifecycle gates; this PR does not claim 100% request coverage.
