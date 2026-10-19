# Backup contract

| Data | Native backup | OMV directory | R2 |
|---|---|---|---|
| PostgreSQL | Barman Cloud | `cloudnativepg-backups` | `cloudnativepg-backups` |
| OpenBao | Raft snapshot adapter | `openbao-backups` | `openbao-backups` |
| Future application PVCs | K8up/Restic | `<namespace>/<repository-pvc>` | Never |
| etcd | native K3s snapshot | `etcd-backups` | `etcd-backups` |
| Prometheus | Native TSDB snapshot, then one-shot K8up Backup | `kube-prometheus-stack/prometheus-k8up-repository` | Never |

All OMV storage is beneath the dedicated `k3s-cluster` Btrfs subvolume on the verified backup filesystem. The Live StorageClass source is exactly `//192.168.1.5/k3s-cluster`. Verify that the backing disk and SMB share are available; never create a substitute directory on the root filesystem. PostgreSQL/OpenBao PVCs are excluded from generic K8up backups. Longhorn replication is not backup.

## Credentials and bucket separation

A single credential object at OpenBao `kv/platform/r2` is read through three separate Kubernetes auth roles and service accounts in `openbao`, `cloudnative-pg`, and `kube-system`. VSO creates distinct destination Secrets in each namespace. Bucket selection remains declarative and separate for each backup path. The Barman `ObjectStore` API cannot reference a Secret for `endpointURL`, so the protected Bootstrap ceremony owns exactly that create-once object and supplies the hidden endpoint without placing it in Git or logs. Its remaining fields reference the VSO-produced `postgresql-r2-credentials` Secret. `backup-validation` has no R2 policy, auth role, Secret, mount, or workload access.

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
