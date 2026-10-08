# Cluster and PostgreSQL maintenance runbook

K3s mutations remain owned by the bootstrap repository; GitOps owns
`Cluster/platform-postgres`. Every mutating live step requires fresh approval.

## Before maintenance

- [ ] Argo CD and CloudNativePG are Healthy/Synced.
- [ ] One primary and two standbys are healthy, distributed and caught up.
- [ ] Synchronous replication is active, or degraded asynchronous operation is explicitly accepted.
- [ ] The latest R2 base backup and WAL archive are current.
- [ ] Longhorn reports three healthy strict-local encrypted volumes on the intended SSDs.
- [ ] No backup, rotation, migration or failure drill overlaps the window.
- [ ] Rollback, abort criteria and independent recovery material are agreed.

Stop if any item cannot be established. Never print Secrets, DSNs or keys.

## Operator or plugin update

1. Review release notes, CRDs and K3s compatibility.
2. Pin chart versions and the `amd64`/`arm64` OCI index digests in Git.
3. Render all charts and manifests locally.
4. Change only operator or plugin, not PostgreSQL, K3s, Longhorn or an app.
5. Verify both leader-elected replicas, certificates and reconciliation.

A Git revert is not a safe CRD downgrade.

## PostgreSQL patch update

1. Take and verify an on-demand base backup in a separately approved operation.
2. Pin the new PostgreSQL image digest in Git.
3. Keep `primaryUpdateStrategy: supervised`; update standbys one at a time.
4. Approve a controlled switchover to an updated standby.
5. Update the former primary last.
6. Verify consumers, synchronous mode, WAL archival and a post-change backup.

A major PostgreSQL upgrade requires a separate blue/green or upstream-supported
migration ticket. Preserve the old cluster, PVCs and backups until independent
verification completes.

## Node maintenance

1. If the target hosts the primary, perform a controlled switchover first.
2. Enable the reviewed CloudNativePG node-maintenance state.
3. Drain or stop exactly one node after separate approval.
4. Return that node and its strict-local Longhorn volume.
5. Wait for its instance to become healthy and fully caught up.
6. End maintenance mode and verify the whole cluster before another node.

For permanent node/SSD loss, do not force-move or delete the old volume. Build a
new encrypted PVC and let CloudNativePG reconstruct the instance from a healthy
PostgreSQL source. Preserve the failed volume until recovery is verified.

## K3s maintenance

Use the bootstrap repository's guarded serial existing-cluster flow. Maintain
one server at a time; a required restart still needs
`k3s_restart_allowed=true`. Verify etcd quorum, CNI/DNS, Longhorn, OpenBao,
CloudNativePG and applications after each node. Do not improvise downgrade,
fencing or multi-node failure tests.

## Backup and restore

- Daily base backup: `03:30`; continuous WAL archive; 30-day recovery window.
- Longhorn replicas and PostgreSQL replicas are not backups.
- K8up must continue to exclude all CloudNativePG PVCs.
- Perform quarterly restore and PITR into separate test resources, never first
  over the running cluster.
- Retain old PVCs and backups until the restored result is independently checked.

## Completion

- [ ] Exactly one primary and two standbys are healthy on distinct nodes.
- [ ] Expected synchronous/degraded mode and alerts agree.
- [ ] Grafana and Radar can use only their own databases within their budgets.
- [ ] WAL archival and a fresh R2 backup succeed.
- [ ] Dashboard signals are present; missing metrics are not shown as healthy.
- [ ] No unexplained Longhorn, certificate or network-policy degradation remains.

Local tests and a successful rollout do not establish HA, failover, backup or
recovery acceptance.
