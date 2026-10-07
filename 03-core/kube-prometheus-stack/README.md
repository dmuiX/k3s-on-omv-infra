# Monitoring after Longhorn

`01-bootstrap/monitoring-crds` renders **only** CRDs from the same pinned Helm chart in
wave 1. The main `kube-prometheus-stack` Helm Application runs in wave 3, after
Longhorn (wave 2). Its values disable CRD ownership, so the two Applications
never own the same CRD. Resource names, namespace, Grafana Service and
Prometheus/Alertmanager CR names stay unchanged. Longhorn's ServiceMonitor can
exist before the operator starts; it will be picked up in wave 3.

| Consumer | Claim | Storage |
| --- | --- | --- |
| Grafana | `kube-prometheus-stack-grafana` | 2Gi, RWO, `longhorn` |
| Prometheus | operator-created claim from `spec.storage.volumeClaimTemplate` | 20Gi, RWO, `longhorn-monitoring`; two replicas on OMV/Wyse; retention 15d / 18GB |
| Alertmanager | operator-created claim from `spec.storage.volumeClaimTemplate` | 1Gi, RWO, `longhorn` |

The `longhorn` class uses three replicas. The dedicated `longhorn-monitoring`
class uses two replicas and the `monitoring-storage` Longhorn node selector; only
OMV and Wyse carry that Git-managed node tag. Both classes are non-default and
encrypt **new** volumes with the separately managed key. `Retain` is not a backup:
reserve disk space and arrange tested off-host backup/recovery separately. Operators create the
Prometheus/Alertmanager StatefulSets and their PVCs; the Helm values define the
corresponding CR storage templates. Grafana renders a PVC directly and uses
`Recreate` upgrades: the old pod stops before the replacement mounts its RWO
volume, avoiding concurrent SQLite writers and cross-node multi-attach stalls.
Grafana upgrades therefore include downtime. Its subchart creates the initial
random administrator Secret `kube-prometheus-stack-grafana` in-cluster. Argo
ignores only that Secret's generated administrator data fields to prevent
render-time randomness from rotating the credential; no administrator value is
committed or logged. Other exporters, rules and operator components do not need
persistent data claims.

The Prometheus claim places two copies of its requested data only on OMV and
Wyse. Grafana and Alertmanager continue to use the normal three-replica class,
so their combined 3Gi requested capacity still has a replica on the Pi. OpenBao
also retains its three-node storage design. Capacity review must use Longhorn's
effective allocatable bytes after the OS, K3s, filesystem and minimum-free-space
reservation. PostgreSQL and future consumers require separate budgets.

`PlatformPVCUsageWarning` fires at 70% and `PlatformPVCUsageCritical` at 85% for
monitoring and OpenBao claims. At warning level, identify the claim and its
recent growth, confirm that metrics are present, and plan a declarative PVC
expansion while free Longhorn capacity remains. At critical level, stop
nonessential data growth and expansion-dependent rollouts. Never delete a PVC,
PV, Longhorn volume, or encryption material as a capacity workaround.

PVC expansion is supported, but PVC shrinking is not. A reduced Git declaration
does not shrink an already-bound claim and may produce drift or an invalid
update. Moving retained data to a smaller claim requires a separately approved
backup/restore or replacement migration; preserve the original volume and its
backup until the replacement is independently verified. Disposable monitoring
history may only be recreated after explicit approval.

## Controlled transition from an installed wave-1 chart

**Do not just enable root auto-sync on a running cluster.** Existing monitoring
uses `emptyDir`/ephemeral TSDB and Grafana storage. Enabling PVCs starts fresh
volumes; it does **not** migrate dashboards, alert state, or metrics history.
Export dashboards/alert configuration to a safe location first if needed; assess
whether any existing claims already exist and what data they contain. Keep the
existing monitoring Application name; this repo only changes its wave, values,
and CRD ownership. Both Applications have auto-prune disabled to prevent CRD
deletion during the handoff; future chart-resource removals also require manual
review. Do not re-enable pruning until the live CRD tracking handoff is
confirmed. This does not guarantee a zero-downtime transition.

Before a separately approved GitOps sync: validate Longhorn prerequisites,
independently back up the encryption key, confirm an encrypted disposable PVC
can be provisioned/mounted/read/reattached, and check capacity for all three
claims plus WAL/compaction headroom. Review the live CRDs, app ownership and
existing PVC metadata without reading Secrets. Seed/verify Argo CD's child-App
health customization before the root sync. During the transition, verify the
CRD-only app owns all monitoring CRDs and is healthy **before** permitting wave 2;
verify Longhorn CSI, class and key availability **before** permitting wave 3.
If Argo reports shared-resource conflicts, stop and review the tracking/ownership
handoff; do not delete CRDs or force-replace them. Monitor PVC binding and pod
readiness before allowing subsequent waves.

Read-only checks after an approved sync (do not print Secret contents):

```sh
kubectl -n argocd get applications monitoring-crds longhorn kube-prometheus-stack
kubectl get crd servicemonitors.monitoring.coreos.com podmonitors.monitoring.coreos.com
kubectl -n kube-prometheus-stack get pvc
kubectl -n kube-prometheus-stack get pods
kubectl -n kube-prometheus-stack get prometheus,alertmanager
```

If claims remain Pending or workloads fail, pause further syncs and diagnose
StorageClass/CSI/key/host capacity and events. Do not delete PVCs, PVs, CRDs or
the encryption key as a rollback shortcut. The StorageClass change governs
replacement claims; Kubernetes cannot change an existing PVC's immutable
`storageClassName`. Update the installed Prometheus Longhorn volume once to two
replicas and the same `monitoring-storage` node selector, then observe it until
both OMV/Wyse replicas are healthy and the Pi replica has been removed. A
rollback needs a reviewed plan for both Application ownership and any new data;
re-enabling ephemeral values does not recover old data. Offline validation: `ruby tests/verify-bootstrap.rb`; the
networked `ruby tests/verify-charts.rb` renders the pinned Helm sources.
