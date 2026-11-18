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

A single credential object at OpenBao `kv/platform/r2` is read through separate namespace-scoped Kubernetes auth roles and service accounts. VSO creates distinct destination Secrets. Bucket selection remains declarative and separate for each backup path. The canonical non-secret endpoint is `r2.endpoint` in private Live's `infra/r2/values.yml`, not public Infra. There are no credentials in that file.

The Barman `ObjectStore` API cannot reference a Secret for `endpointURL`. The `k3s-postgres` Argo Application renders the complete `cloudnative-pg/k3s-postgres-backups` ObjectStore from `charts/barman-objectstore` before the Cluster. Its existing Live raw source still renders `infra/k3s-postgres` and also resolves `$live/infra/r2/values.yml` from the Live repository root. Missing or empty endpoints (including the initial `""` sentinel) fail rendering. The only accepted form is HTTPS with a 32-character lowercase hexadecimal account ID, optional `.eu` or `.fedramp` jurisdiction, and the exact R2 hostname suffix. No trimming or normalization is applied; trailing LF, CR, or spaces are rejected. Access key, secret key and region are references to the existing VSO-produced `postgresql-r2-credentials` Secret, not Helm inputs.

The paired Bootstrap revision no longer seeds or hands off the ObjectStore. It still loads the canonical endpoint from the controller-local sibling Live checkout at `{{ playbook_dir }}/../../k3s-on-omv-live/infra/r2/values.yml` before GitOps changes, for backup credential setup and OMV rclone custody. Create-once KV-v2 CAS 0 and resume checks for protected OMV rclone custody remain. The endpoint stays in `kv/platform/r2` for existing consumers; neither endpoint nor credentials are rotated by this adoption.

### Ordered adoption (not authorization)

1. **Live:** publish the valid endpoint matching the existing ObjectStore, credentials and OMV configuration before Infra consumes it. A missing value or mismatch blocks adoption; do not use it to change endpoints.
2. **Infra:** adopt the same ObjectStore identity in the existing Application by SSA, with client-side migration disabled. `Prune=false` and `Delete=false` retain it; the legacy `k3s-on-omv.io/create-once-owner` annotation is kept only for compatibility. Sidecar requests stay **50m/64Mi** and limits **250m/256Mi**; the old unactivated 250m/192Mi request target is not part of adoption. Do not add force or replacement sync options, delete/recreate the object, or run the legacy Bootstrap ObjectStore path during this transition.
3. **Paired Bootstrap:** after Infra adoption is verified, use only the revision that removes ObjectStore seed/handoff and still consumes Live for credentials and rclone.

**Before merging Infra for an existing cluster**, verify the exact candidate revisions and arrange an attended reconciliation hold on both `infra` and `k3s-postgres`, with no pending or active operations. Both track `main` and may auto-sync on merge; a post-merge check is too late without the hold. Freeze concurrent syncs and legacy Bootstrap runs through adoption. Verify the complete render and functional SSA (including admission, unchanged spec and sizing, and stable identity); Argo's SSA can take conflicting ownership, so do not rely on a conflict to stop drift. The endpoint is not a credential, but it will be visible to authorized Argo desired-manifest readers. Verify that the existing audience may see it and keep it out of public outputs.

With both holds still in place after merge, compare the live child Application against the reviewed merged manifest. If the old sources are still installed, separately approve a resourceVersion-guarded update of only the existing `k3s-postgres` Application's `spec.sources`, using the exact merged source list. Preserve both holds; do not replace the Application or release the root just to deliver that list. Read back the child, verify the full source list, unchanged remaining spec and idle state, then authorize child reconciliation separately. After the child sync, verify the unchanged ObjectStore UID, Argo tracking, no unexpected Pod rollout, and fresh WAL/archive and backup evidence without reading credential values; only then release the root. Stop on errors or drift; retain the object and coordinate any source rollback rather than reintroducing the old partial desired state or resuming the old Bootstrap writer. This change authorizes no live operations.

Future R2 egress restriction is not implemented. It remains pending the real endpoint and validation of native Istio waypoint enforcement; canonical endpoint configuration alone is not egress isolation.

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
