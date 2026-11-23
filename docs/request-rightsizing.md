# Measured native request reductions — no automatic sizing

This request-only stage (PR90) reduced only CPU/RAM **requests** through existing chart fields
and the existing missing-only ServiceLB policy. All limits, chart versions,
replicas, placement/anti-affinity, storage membership, identities, networking,
credentials and endpoint custody remain unchanged. No VPA, new controller,
namespace default, live-Pod patch or additional monitoring stack is introduced.
The later [native RAM-limit profile](memory-limit-profile.md) changes caps
separately without changing these requests; its own startup/lifecycle gates apply.

## Profiles (per container)

| Component | Old CPU / RAM | New CPU / RAM | Native resources path |
|---|---|---|---|
| Argo repo-server and fallback copyutil |100m /128Mi|50m /128Mi|repoServer.resources|
| Argo server |50m /128Mi|20m /128Mi|server.resources|
| ztunnel |200m /128Mi|150m /64Mi|resources in ztunnel chart|
| Istiod |100m /256Mi|25m /224Mi|resources in istiod chart|
| Istio CNI |100m /100Mi|25m /100Mi|resources in cni chart|
| Barman plugin CONTROLLER |50m /96Mi|20m /80Mi|resources in plugin-barman-cloud chart|
| cert-manager controller |50m /64Mi|10m /64Mi|resources|
| Crossplane RBAC main AND init |100m /256Mi|10m /64Mi|resourcesRBACManager|
| K8up operator |50m /96Mi|10m /80Mi|resources|
| OpenBao server |100m /256Mi|100m /224Mi|server.resources|
| VSO manager |50m /96Mi|20m /96Mi|controller.manager.resources|
| ServiceLB TCP containers |5m /8Mi missing-only default|5m /4Mi missing-only default|existing CREATE admission policy|

Crossplane main remains100m/256Mi, distinct from its small RBAC manager. The
Barman DATABASE INSTANCE sidecar remains250m/192Mi with250m/256Mi limits; it is
not the plugin controller. V1 instance-manager CPU/RAM448Mi Pi /640Mi standard
is NOT reduced. No storage engine CPU guarantee or data replica is removed.
CoreDNS/Metrics Server CPU suggestions remain outside this Infra change because
K3s/Bootstrap owns them; no ownership migration or ad-hoc patch for125m CPU.

## Measurement evidence and limits

Read-only2026-11-17 snapshot:94 active Pods,139 containers including all11 ordinary
init and3 restartable-init sidecars. Both Prometheus replicas queried explicitly
at the same epoch; no round-robin mixing. Historical quantile<=peak checked.
Normal workload CPU/memory P95 is calculated after aggregating container IDs per
Pod,7d/5m observations with >=12h coverage; raw memory peaks retain short series.
All current replicas and sufficiently observed predecessor Pods considered.
This is not uninterrupted seven-day recovery/failover or a hard high-water bound.

| RAM reduction | Normal P95 / raw peak MiB | Target |
|---|---:|---:|
| ztunnel |31.0 /43.9|64Mi|
| Crossplane RBAC |41.3 /52.2; init sampled53.4|64Mi|
| Istiod |158.6 /178.5|224Mi|
| Barman plugin controller |43.7 /64.2|80Mi|
| K8up operator |55.8 /63.9|80Mi|
| OpenBao server |180.9 /196.2|224Mi|
| ServiceLB, highest TCP family |2.03 /2.72|4Mi|

Requests are reservations, not hard caps; this request-only stage retained burst
limits/no-limit behavior. Crossplane64Mi also changes its init request: sparse
historical samples do not prove its worst-case startup high-water. The separate
[RAM-limit profile](memory-limit-profile.md) bounds RBAC main/init to128Mi and
requires init readiness/startup acceptance, not a generic64Mi cap. OpenBao needs serial raft/unseal/
snapshot recovery acceptance. The current third-server recovery after the user's
Pi reserve activation is NOT proof that this reduced224Mi profile is adopted.

No cuts to PostgreSQL, Prometheus/Grafana main, Crossplane main, CSI/native
storage or the Barman instance sidecar: observed usage/backup/recovery does not
show a defensible excess. A Pi Barman OOM at its unchanged256Mi cap and raw CSI
plugin/helper peaks around948/1040Mi are separate concerns, not reasons to lower
limits or blindly reserve every rare peak as permanent request. Host and ancestor
cgroup headroom, startup/surge and recovery remain operational gates.

## Real source/render regression boundaries

Three new tests first fail against unchanged main, then exercise the actual exact
Helm versions and both normal/init consumers. A whole-object comparison restores
only old request inputs and normalizes only the intended named request maps.
The pinned istiod chart also echoes resources into ConfigMap values original-
values/merged-values JSON: only that same resources.requests map is normalized;
all other ConfigMap data, injection/security fields and limits remain compared.
Existing typo/fallback/HA/storage contracts remain. Real isolated API tests check
the4Mi ServiceLB default with preserved explicit/zero/limit-derived resources,
selector exclusions and no UPDATE/init mutation. No production API mutation.

## Capacity arithmetic — not complete-profile acceptance

The source-placement model retains one CSI controller per family/node, the SMB
controller on a standard node, OpenBao/helper on Pi, and other observed placement.
Native Pi target **2204 ->2108Mi**, with CPU **1945 ->1820m**. With effective992Mi
kubeReserved and unchanged system/eviction protection, allocatable is~2337.664Mi:
native request spare~229.664Mi (formerly133.664Mi). No further reserve cut here.

The existing IM/ServiceLB admission target still adds448Mi +3*4Mi=460Mi:
**2568Mi /1835m**, still **230.336Mi over allocatable** and **358.336Mi short
including128Mi spare**. This change improves headroom; it does NOT make all
configured requests fit the Pi. Do not treat a green CI as that approval.

Total additional request potential:612Mi cluster /108Mi Pi versus the previous
configured profile, conditional on shared init/recovery acceptance. The already
authored live512->128Mi ztunnel reduction and SMB movement are NOT counted twice.
CPU savings in this PR:870m cluster /125m Pi; the125m K3s-owned CPU opportunity
was deliberately excluded. CPU savings do not fix a RAM admission deficit.

The Bootstrap2204Mi reserve-safety projection is intentionally conservative after
these source reductions and remains unchanged. Fresh admitted Pods, eligible
placement, all Pending work, ordered init, overhead, surge/failover and actual
physical/ancestor-cgroup headroom must be checked before activation. Missing
engine-image/helper/UI/deployer request fields are not silently filled here.

## Activation / rollback boundary

Main auto-reconciles: merge can roll mesh/controller/server Pods. Existing native
CSI/DB changes can also continue when the previously blocked root sync recovers;
this PR is not permission for them. No manual sync/restart/switchover is included.
Keep the separate admission full-profile capacity blocker visible. Do not enable
all future reservations merely because OpenBao is now Ready.

Rollback is a reviewed revert of the request values and another controlled
reconciliation, not an immediate undo of born Pods. The ServiceLB default only
affects new matching Pods; existing admitted values are preserved. With
`prune: true` (`PruneLast=true`) on `resource-defaults`, removing the admission
bindings from Git prunes them on sync; follow the verification checks in
`scoped-resource-admission.md`. No blind Pod delete.
