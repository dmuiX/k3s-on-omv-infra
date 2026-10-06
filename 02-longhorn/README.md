# Longhorn: bootstrap settings and encrypted volumes

## Structure and ordering

Folder prefix `02-` denotes Longhorn's controller deployment wave. Its UI
route is a separate wave-6 Application rendering `charts/cluster-config/` with
private Git values; placing it
after certificate configuration avoids blocking Longhorn on TLS readiness. The
namespace and release name remain `longhorn`. HTTPS only becomes usable once
the wildcard certificate is ready.

- `app.yml`: pinned Longhorn Helm chart, Git values and final StorageClass ConfigMap override.
- `values.yml`: three-replica storage defaults; no embedded Flux HelmRelease.
- `storageclass-configmap.yaml`: reviewed final Argo source overriding the chart's
  `longhorn-storageclass` ConfigMap; the normal `longhorn` class encrypts new
  volumes by default. No passphrase
  or Kubernetes Secret is committed here.
- The public route template selects `longhorn-frontend:80` through the K3s
  Gateway; only the real hostname value comes from private Git.

The previous Flux HelmRelease/HelmRepository files and unused HelmRelease schema
are removed. Argo CD is the only deployment controller for Longhorn.

| Wave | Dependency |
| ---: | --- |
| 1 | Child-Application health customization; monitoring CRDs only |
| 2 | Longhorn controller/CSI, encrypted `longhorn` class template and ServiceMonitor |
| 3 | Persistent monitoring and OpenBao; their PVCs select `longhorn` |
| 4 | Vault Secrets Webhook |
| 5 | Cloudflare credential, issuer, wildcard certificate |
| 6 | Longhorn UI route rendered with the private hostname value |

The CRD-only Application renders the same pinned monitoring chart as the full
stack, with all workloads disabled. Longhorn can create its ServiceMonitor
in wave 2; the Prometheus operator and storage-backed Prometheus start in wave 3.
Prometheus selects ServiceMonitors and PodMonitors across namespaces/releases,
including Longhorn's metrics Service. See
[`03-kube-prometheus-stack/README.md`](../03-kube-prometheus-stack/README.md)
for the handoff from existing ephemeral monitoring.

## The normal `longhorn` class encrypts new volumes

New dynamically provisioned PVCs using **`storageClassName: longhorn`** get V1
LUKS/dm-crypt encryption, ext4, expansion, three replicas and `Retain`. There is no
second encrypted class. **`local-path` remains the cluster default** for PVCs
without a class; manually created UI volumes are outside this StorageClass rule.
Existing volumes are **not** converted, even though their class name is unchanged.

Longhorn 1.11.1 does not expose encryption/CSI key references in its Helm values.
The Application therefore uses `storageclass-configmap.yaml` as its final Argo
source, intentionally overriding the chart's ConfigMap. Argo may report a
`RepeatedResourceWarning`; the last source is the desired owner. Do not deploy
with a standalone `helm upgrade -f values.yml`, because that omits the encrypted
override. `verify-longhorn-encryption.rb` checks the source order and template.
Update both values and override when changing replica count, filesystem or retention.

**Rollout changes an existing class.** Longhorn's ConfigMap controller detects the
changed template and **deletes/recreates `StorageClass/longhorn` itself**; direct
Kubernetes parameter patching would fail because the fields are immutable. Do not
manually race this controller with another StorageClass owner. Freeze new PVC
creation during the approved transition. Existing PVs/PVCs and their data remain
in place. The ConfigMap has `Prune=false`; no destructive Argo sync hook or
`Force/Replace` sync option is used.

**Before publishing/syncing**, create and independently back up
`longhorn/longhorn-volume-encryption`. All four CSI Secret references (including
node expansion) use namespace `longhorn`. The operator runbook is
[bootstrap volume-encryption guide](../../k3s-on-omv-bootstrap/2%20longhorn/volume-encryption.md); it includes key custody,
target checks and an isolated encrypted-volume test. The key is not retrieved
from OpenBao: OpenBao must not be needed to unlock its own storage. Healthy K3s
Secret encryption is a prerequisite. Keep the key while any volume or backup
requires it.

Encryption alone does not provide storage HA. The three-replica setting requires
adequate disk capacity and healthy schedulable storage on all three nodes. Migrating the existing OpenBao data and audit
PVCs is a separate, downtime-requiring change; see
[OpenBao PVC migration plan](../../k3s-on-omv-bootstrap/5%20k3s/migrations/openbao-encrypted-pvcs.md). Do not initialize a new
OpenBao cluster merely to change the underlying storage encryption.

## Three-node replica settings

- PVC/StorageClass replica count: **3** for newly provisioned volumes.
- OpenBao runs **three Raft server pods** with separate data and audit PVCs;
  each newly created Longhorn volume starts with three storage replicas.
- UI-created volume defaults: **3** for each data engine (does not enable V2).
- Reclaim policy: **Retain**. Released volumes are not automatically reclaimed;
  cleanup/reuse needs deliberate review. This is not a backup.
- StorageClass `longhorn` is **not default**. Existing K3s `local-path` remains the
  default; consumers opt into Longhorn with `storageClassName: longhorn`.
- UI Service: **ClusterIP**, because the shared Gateway provides LAN/VPN access.
- Pod Security `privileged` labels target the Longhorn namespace through Argo's
  `managedNamespaceMetadata` and `CreateNamespace=true`, not Application `spec.labels`.
- Manager requests: 100m CPU / 256Mi RAM; memory limit: 512Mi. Instance-manager CPU
  reservation remains 12% per configured setting; measure total CSI/engine/replica
  overhead rather than treating the manager's request as the whole storage budget.

Three nodes provide useful failure tolerance only when all volume replicas are
healthy on distinct nodes. Increase replica counts on **existing** volumes
separately; changing defaults affects only new volumes. Scaling OpenBao from one
Raft pod to three creates four additional PVCs (data and audit for each new pod);
neither pod placement nor Raft quorum is proven just by changing a value. Verify
disk capacity, resource budgets, CSI availability and Raft membership during rollout.
The current `createDefaultDiskLabeledNodes: "false"` permits default disk
creation on joining nodes; do not assume Pi/Wyse storage will remain excluded
without changing that policy.

## Mandatory checks before deployment

These are not performed by chart rendering and have not been signed off by the
local configuration checks:

1. Verify the installed K3s/kernel combination against Longhorn's requirements.
   Check iSCSI utilities/service, required kernel support and any NFS dependencies
   for the selected functionality. Run a reviewed version-matched preflight check.
2. Approve `/var/lib/longhorn` and its backing filesystem/device, free capacity,
   disk pressure thresholds and backup location. The value in Git is not approval
   to start writing storage data onto the OMV host.
3. Review the rendered namespace/privileged workloads, manager resource settings,
   and Argo auto-prune/self-heal flags. Inventory existing Longhorn PVCs before
   any deployment; changing a StorageClass does not migrate existing consumers.
4. Provision/back up the independent key before the separately approved sync.
   Verify controllers/CSI and the reconciled `longhorn` StorageClass: three replicas,
   `Retain`, `encrypted: "true"` and all four CSI key-reference pairs.

Read-only checks after installation:

```sh
kubectl -n longhorn get pods -o wide
kubectl get csidrivers
kubectl get storageclass longhorn -o yaml
kubectl -n longhorn get nodes.longhorn.io
kubectl -n longhorn get servicemonitor
```

Then, in an approved disposable test namespace: provision a small PVC explicitly
using Longhorn, mount it, write a known test payload, flush it, recreate the test
consumer against the same claim and verify its checksum. Check the replica count
and volume health. Review cleanup separately because `Retain` leaves data behind.
Do not use real OpenBao data as the first storage test. Backup/restore testing is
a separate gate; three replicas still require verified placement and rebuild behavior.

Longhorn's chart renders the StorageClass specification inside a ConfigMap for
its components to create. Seeing that ConfigMap or an Argo `Healthy` status does
not by itself prove working CSI provisioning/mounts.

If a fresh-install test fails, stop subsequent syncs and diagnose; do not blindly
uninstall the chart, delete its namespace/CRDs, or remove the host data path. Once
volumes exist, recovery/rollback must preserve them and follow Longhorn's supported
upgrade/recovery procedures rather than an assumed safe version downgrade.
