# Safe Pre-Drain Procedure for CloudNativePG & Longhorn

This runbook documents why `kubectl drain` is intentionally blocked on nodes hosting single-replica (`strict-local`) Longhorn volumes or the CloudNativePG primary, how to distinguish expected Radar PDB warnings from real maintenance blockers, and the exact steps to follow before draining or rebooting a cluster node.

## 1. Why Drain Blockers Exist (Do Not Relax Them)

Two deliberate safety mechanisms produce `ALLOWED DISRUPTIONS: 0` `PodDisruptionBudget` objects on this cluster:

1. **Longhorn `node-drain-policy: block-if-contains-last-replica`**:
   - Longhorn creates an `instance-manager-*` PDB (`minAvailable: 1`, `ALLOWED DISRUPTIONS: 0`) on every node that currently holds the **last healthy replica** of any attached volume.
   - Because self-replicating workloads use single-replica `strict-local` StorageClasses (`longhorn-single-node-retain` and `longhorn-single-node-omv-wyse-retain`), every node holding one of those volumes has an active `instance-manager` PDB.
   - **Never** relax `node-drain-policy` globally or delete Longhorn `instance-manager` PDBs just to silence dashboard warnings.
2. **CloudNativePG `k3s-postgres-primary` PDB**:
   - Protects the active PostgreSQL primary from uncoordinated eviction until a controlled switchover moves the primary role to another healthy standby.

### Expected `strict-local` Volumes by Node

| Workload | StorageClass | Replicas / Locality | Typical Nodes | Replication Layer |
| --- | --- | --- | --- | --- |
| `cloudnative-pg/k3s-postgres-{1,2,3}` | `longhorn-single-node-retain` | `1` / `strict-local` | All 3 nodes (1 per node) | PostgreSQL synchronous quorum streaming replication |
| `kube-prometheus-stack/prometheus-{0,1}` | `longhorn-single-node-omv-wyse-retain` (legacy: `longhorn-monitoring`) | `1` / `strict-local` | `omv`, `wyse5070` | Independent dual-Prometheus HA scraping |
| `kube-prometheus-stack/alertmanager-*` | `longhorn-monitoring` (retained) | `1` / `strict-local` | Detached (`replicas: 0`) | Intentionally scaled to `0` |

> Replicated workloads using `longhorn-replicated-retain` (3 replicas, `dataLocality: disabled`) are **not** drain blockers as long as at least 2 replicas remain healthy on other nodes.

---

## 2. Pre-Maintenance Verification Checklist

Run these checks **before** cordoning or draining any node (`<TARGET_NODE>`):

### A. Verify Cluster & Storage Health
```bash
# 1. Confirm all 3 nodes are Ready (never drain a second node while another is degraded)
kubectl get nodes

# 2. Check all Longhorn volumes: no degraded 3-replica volumes, identify strict-local volumes on <TARGET_NODE>
kubectl -n longhorn get volumes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,STATE:.status.state,ROBUSTNESS:.status.robustness,REPLICAS:.spec.numberOfReplicas,LOCALITY:.spec.dataLocality,NODE:.status.currentNodeID,PVC:.status.kubernetesStatus.pvcName,NS:.status.kubernetesStatus.namespace'
```
- **Stop** if any `3`-replica volume is `degraded` — wait for rebuild completion before draining.

### B. Verify CloudNativePG Quorum & Replication
```bash
# 1. Check cluster phase, primary, and ready instances (must be 3/3 Healthy)
kubectl -n cloudnative-pg get cluster k3s-postgres -o wide

# 2. Verify both standbys are actively streaming with 0 lag and reserved WAL slots
PRIMARY=$(kubectl -n cloudnative-pg get cluster k3s-postgres -o jsonpath='{.status.currentPrimary}')
kubectl -n cloudnative-pg exec "$PRIMARY" -c postgres -- \
  psql -U postgres -c "SELECT application_name, state, sync_state, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)) AS lag FROM pg_stat_replication;"
kubectl -n cloudnative-pg exec "$PRIMARY" -c postgres -- \
  psql -U postgres -c "SELECT slot_name, active, wal_status FROM pg_replication_slots;"
```
- **Stop** if either standby is not `streaming` or if any replication slot has `wal_status = lost` (see [postgresql-wal-recovery.md](postgresql-wal-recovery.md)).

### C. Verify OpenBao & etcd Quorum
```bash
# OpenBao must have 3/3 Ready pods (Raft quorum requires 2/3)
kubectl -n openbao get pods -o wide

# Verify Prometheus has 2/2 Ready replicas if draining omv or wyse5070
kubectl -n kube-prometheus-stack get pods -l app.kubernetes.io/name=prometheus -o wide
```

---

## 3. Controlled Drain Procedure

1. **Cordon the target node** so no new pods schedule onto it:
   ```bash
   kubectl cordon <TARGET_NODE>
   ```
2. **If `<TARGET_NODE>` hosts the CloudNativePG primary**, perform a controlled switchover first:
   ```bash
   # Pick a healthy standby pod on another node (e.g. k3s-postgres-2)
   kubectl cnpg promote k3s-postgres <TARGET_STANDBY_POD> -n cloudnative-pg
   # Wait until .status.currentPrimary updates and the cluster returns to Healthy
   kubectl -n cloudnative-pg get cluster k3s-postgres -w
   ```
3. **Evict standard workloads**:
   ```bash
   kubectl drain <TARGET_NODE> \
     --ignore-daemonsets \
     --delete-emptydir-data \
     --disable-eviction=false
   ```
   - `kubectl drain` will safely evict all replicated workloads (`cert-manager`, `cloudnative-pg` operator, `k8up`, `openbao` pod, etc.).
   - It will **intentionally pause** on the node's `strict-local` workloads (`k3s-postgres-*` standby, `prometheus-*` on `omv`/`wyse5070`) and the Longhorn `instance-manager` PDB.
4. **For a planned reboot or short maintenance window (< 15 min)**:
   - Once all non-local workloads have moved off `<TARGET_NODE>` and the CNPG primary is on another node, **do not force-delete the `instance-manager` PDB**.
   - Gracefully stop/reboot the node directly (k3s/kubelet cleanly terminates the remaining local standby pods and detaches local volumes; CNPG quorum continues uninterrupted on the remaining 2 nodes, and Prometheus continues scraping on the peer replica).
5. **Post-maintenance recovery**:
   ```bash
   kubectl uncordon <TARGET_NODE>
   # Confirm node Ready, CNPG 3/3 streaming, OpenBao 3/3 unsealed, Longhorn all healthy
   kubectl get nodes
   kubectl -n cloudnative-pg get cluster k3s-postgres
   kubectl -n openbao get pods -o wide
   kubectl -n longhorn get volumes.longhorn.io -o custom-columns='NAME:.metadata.name,STATE:.status.state,ROBUSTNESS:.status.robustness,PVC:.status.kubernetesStatus.pvcName'
   ```

---

## 4. Expected Radar Warnings vs. Actionable Alerts

- **Expected (suppressed via namespace `radarhq.io/ignore-checks`)**:
  - `kube-prometheus-stack` Alertmanager `serviceNoMatchingPods` while `alertmanagerSpec.replicas: 0`.
  - Longhorn `instance-manager` PDB `0 allowed disruptions` while `block-if-contains-last-replica` protects `strict-local` volumes.
- **Actionable (investigate immediately)**:
  - Any `3`-replica Longhorn volume in `degraded` state.
  - Any `k3s-postgres` standby not in `streaming` state or with `wal_status != reserved`.
  - Any `Service has no matching pods` outside intentionally scaled-to-zero Alertmanager.
