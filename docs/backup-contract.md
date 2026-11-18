# Backup contract

| Data | Native backup | OMV directory | R2 |
|---|---|---|---|
| PostgreSQL | Barman Cloud | `cloudnativepg-backups` | `cloudnativepg-backups` |
| OpenBao | Raft snapshot adapter | `openbao-backups` | `openbao-backups` |
| Future application PVCs | K8up/Restic | `<namespace>/<repository-pvc>` | Never |
| etcd | native K3s snapshot | `etcd-backups` | `etcd-backups` |
| Prometheus | Native TSDB snapshot, then one-shot K8up Backup | `kube-prometheus-stack/prometheus-k8up-repository` | Never |

All OMV storage is beneath the dedicated `k3s-cluster` Btrfs subvolume on the verified backup filesystem. The Live StorageClass source is exactly `//192.168.1.5/k3s-cluster`. Verify that the backing disk and SMB share are available; never create a substitute directory on the root filesystem. PostgreSQL/OpenBao PVCs are excluded from generic K8up backups. Longhorn replication is not backup.

For the November 2026 WAL-retention incident, recovery gates and the planned December CNPG update follow-up, see [PostgreSQL WAL recovery](postgresql-wal-recovery.md).

## Credentials and bucket separation

A single credential object at OpenBao `kv/platform/r2` is read through separate namespace-scoped Kubernetes auth roles and service accounts. VSO creates distinct destination Secrets. Bucket selection remains declarative and separate for each backup path.

Argo owns the complete `cloudnative-pg/k3s-postgres-backups` ObjectStore as a raw manifest in the existing `k3s-postgres` Application, ahead of the Cluster. The ObjectStore omits `configuration.endpointURL`, which takes a literal, not a Secret reference. The Barman Cloud plugin v0.15.1 copies `instanceSidecarConfiguration.env` to the Barman containers in instance Pods and recovery Jobs. `AWS_ENDPOINT_URL_S3` uses a required reference to `postgresql-r2-credentials:S3_ENDPOINT`; the access key, secret key and region references are unchanged. The endpoint comes from OpenBao through the existing VSO destination, not Git or Argo values. The existing Live raw source for PostgreSQL policy and retention is unchanged and supplies no endpoint.

For this PostgreSQL destination only, VSO rejects missing, empty or malformed endpoint inputs without logging them. The exact accepted form is an HTTPS R2 account URL with a 32-character lowercase hexadecimal account ID, optional `.eu` or `.fedramp` jurisdiction and the exact R2 hostname suffix. No extra URL components, trailing LF, CR or spaces are accepted. Render errors block a Secret update; they do not clear an existing Secret or update running Pod environments. Check successful VSO reconciliation, not just Secret presence. On a fresh installation, establish OpenBao custody and the VSO destination before starting backup/recovery workloads. The paired Bootstrap revision must retire ObjectStore seed/handoff while preserving OpenBao and OMV rclone custody, including KV-v2 CAS 0 and resume checks. This is not endpoint or credential rotation.

### Ordered adoption (not authorization)

**Do not merge and auto-sync directly on an existing cluster.** Barman may fall back to AWS S3 if the explicit endpoint disappears before existing containers have adopted the environment reference. A Pod's environment does not change in place. Each phase requires separate approval; this document authorizes no live operations.

1. **Bridge with the existing endpoint retained.** Before merging Infra, verify exact candidate revisions and hold reconciliation of both the root `infra` and child `k3s-postgres` Applications. Both track `main`; a hold after merge is too late. Verify the holds and no active or pending operations. Freeze all competing ObjectStore writes, including manual syncs and legacy Bootstrap, and freeze updates to the R2 custody data during the bridge. Pause the scheduled PITR validator and drain in-flight runs; hold its `backup-validation` Application as needed so reconciliation cannot undo the pause. The old validator copies the production ObjectStore spec into a different namespace and cannot use the new production Secret reference there. Check the current UID, field managers and expected spec (including the presence of the explicit endpoint), and test the full candidate render/admission and SSA in isolation without force or replacement sync options. Confirm from protected custody evidence that the existing endpoint and the VSO source match; stop on missing or ambiguous evidence. With a separately approved UID/resourceVersion-guarded in-place update, add only the required env reference to the current ObjectStore and retain its explicit endpoint and other fields. Read back only identity, field presence, references and sizing. Do not delete/recreate the object.

   Prove adoption in the generated Pod specs: all Barman backup/WAL sidecars and recovery containers must have exactly one `AWS_ENDPOINT_URL_S3` with the required Secret reference and no conflicting override. Coordinate recreation if needed, checking replicas and quorum as the rollout proceeds and completing the approved CNPG supervised primary rollout. Check the running primary as well as the replicas; a new ObjectStore spec is not evidence that old Pods changed. Recreate or drain any pre-bridge recovery Jobs before cutover. With the explicit endpoint still in production, exercise the **env-only** candidate and pinned images against an isolated R2 test target: prove fresh WAL archival, a completed backup and restore/PITR into a new target with the restored data verified. Neither the scheduled PITR validator (which supplies its own explicit endpoint) nor a restore using a historical recorded endpoint proves the env-only path.

2. **Remove the literal and adopt the whole object.** Only after those gates pass, merge with both holds still active. The Application source list is unchanged by this release. Verify the holds, that the existing child source list matches the merged revision, and that no operation is pending; investigate any difference before proceeding. Then separately authorize a controlled child sync. SSA must adopt the same UID, with client-side migration disabled; `Prune=false` and `Delete=false` retain the object and the legacy custody annotation is kept for compatibility. Requests stay **50m/64Mi** and limits **250m/256Mi**. SSA omission may preserve an endpoint field owned by another manager. Check that the field is actually absent; if retained, remove only that field by a separately approved UID/resourceVersion-guarded update while the holds remain in place. Do not rely on SSA conflicts to prevent drift.

   Before resuming automation, verify Argo tracking on the same UID, no endpoint field, healthy VSO reconciliation, unchanged sizing, required refs in all generated containers, and fresh WAL/archive and backup evidence. Verify recovery evidence in isolation, not by overwriting production. Only then restore normal reconciliation and use the paired Bootstrap revision with no ObjectStore writer. Before resuming the scheduled PITR validator, verify it has the updated script that does not copy the production endpoint reference and binds the S3 selectors to its existing local Secret. Do not print Secret payloads, the endpoint, full ObjectStore diffs or environment dumps during either phase.

**Rollback:** Before cutover, stop with the explicit endpoint retained until env adoption is verified; keep the reconciliation holds if the candidate would remove it. While the bridge is installed, do not resume the legacy PITR validator. After cutover, re-hold both Applications, freeze competing writes, and reintroduce the same explicit endpoint from protected custody only via a separately approved, guarded change to the same ObjectStore. Keep the env reference, verify fresh WAL/backup and isolated recovery evidence, and keep automation held until desired state and the approved recovery plan agree. Do not reapply the old partial SSA manifest or resume the legacy Bootstrap writer.

No R2 egress restriction is introduced. Configuring the client endpoint is not network isolation.

SMB credentials are separate at `kv/platform/smb`. A dedicated `kube-system` VSO identity materializes `smb-credentials`; the `smb` StorageClass references it for both provisioning and node staging. No SMB credential is stored in either repository.

## Supported K8up transport

The official K8up chart `4.10.0` uses K8up `v2.16.0`. Its API supports `backend.local.mountPath`, `backend.volumeMounts`, `PodConfig`, and one-shot `Backup`, `Check`, and `Prune` resources. The official `csi-driver-smb` chart is pinned to `1.20.3`; its exact values and StorageClass secret parameters were checked against `/tmp/csi-driver-smb-v1.20.3`.

Live's validation PVC and Prometheus repository PVC are dynamically provisioned by StorageClass `smb`, are `ReadWriteMany`, use Kubernetes reclaim policy `Retain` plus driver parameter `onDelete: retain`, and are annotated `k8up.io/backup: "false"` to prevent recursive backup. The inspected chart/driver API supports `subDir`; `${pvc.metadata.namespace}/${pvc.metadata.name}` deterministically maps each retained repository beneath the share. K8up mounts each as its local Restic repository. There is no NFS PV, hostPath, or R2 fallback. Validate SMB dialect negotiation, CSI behavior on every eligible node, retained-subdirectory ownership, UID/GID 1000 permissions, locking, pruning, node loss, and isolated restore before approval.

The validation Schedule keeps 7 daily, 4 weekly, and 6 monthly successful snapshots. A canary PVC is not a restore test: seed known data, back up/check, restore to a new isolated PVC, and compare checksums. Never overwrite production.

## Prometheus snapshot-first workflow

Prometheus admin API access is enabled. K8up's `PreBackupPod` calls the replica-0 native snapshot API, validates the returned snapshot name, and streams only that snapshot directory to Restic. Its Backup selector matches only the `PreBackupPod`, never the live Prometheus PVC. The exact tool image digest and runtime attachment behavior remain deployment blockers; no image tag or digest is invented.

K8up 2.16 has no conditional post-backup cleanup hook. Therefore cleanup is separate and fail-closed: require `Completed=True` with `reason=Succeeded` on both a uniquely named one-shot Backup and Check, and verify the new K8up Snapshot record before deleting the source snapshot. On any failure, retain it. See [the Prometheus runbook](prometheus-tsdb-backup-runbook.md) for exact native resource templates and the blocked cleanup contract. Restic retention is a one-shot Prune with 7 daily and 4 weekly snapshots.

## Host-side mirrors and remaining recovery gates

Bootstrap defines three mounted-disk-gated systemd services on OMV. Each runs non-destructive `rclone copy` followed by `rclone check --one-way` from `cloudnativepg-backups`, `openbao-backups`, or `etcd-backups` into the same bucket-named directory. Destination ConfigMaps describe this contract but do not perform copies.

Runtime copy evidence, alerts, retention behavior, native K3s S3 upload, isolated restores, and checksum custody are verified by deployed jobs and operational checks rather than pre-deployment gates. Keep R2 credentials (or reissuance access), OpenBao seal/recovery material, the K3s server token, and the K8up repository password in independent encrypted custody. Restore OpenBao first when possible; PostgreSQL and etcd must remain recoverable with an external or reissued R2 credential when Kubernetes and OpenBao are unavailable. Repository changes do not modify the OMV host, cluster, R2, or OpenBao.
