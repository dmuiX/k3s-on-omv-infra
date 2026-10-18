# Longhorn: bootstrap settings and encrypted volumes

## Structure and ordering

Folder prefix `02-` denotes Longhorn's controller deployment wave. Its UI
route is rendered natively by the pinned Longhorn chart, with only its hostname
coming from protected Git values. The namespace and release name remain
`longhorn`. HTTPS only becomes usable once the wildcard certificate is ready.

- `app.yml`: version-pinned Longhorn Helm chart plus branch-tracking same-repository
  Git values and authored resources for node annotations and encrypted StorageClass configuration.
- `values.yml`: three-replica storage defaults; no embedded Flux HelmRelease.
- `encrypted-storageclass-template.yaml`: reviewed final Argo source overriding the chart's
  `longhorn-storageclass` ConfigMap; the normal `longhorn` class encrypts new
  volumes by default. No passphrase or Kubernetes Secret is committed here.
- `prometheus-storage-placement.yaml`: metadata-only core Kubernetes Nodes for OMV and Wyse.
  In intra-Application wave `-1`, server-side apply adds Longhorn's
  official `node.longhorn.io/default-node-tags` annotation before chart resources.
  `Prune=false` and `Delete=false` prevent Argo from deleting cluster Nodes.
- `prometheus-storageclass.yaml`: encrypted two-replica Prometheus StorageClass. Its
  node selector uses the `monitoring-storage` tags that Longhorn initializes from
  the Kubernetes Node annotations.
- `prometheus-scrape-access.yaml`: additive cross-namespace Prometheus access to
  manager metrics on TCP 9500. Longhorn 1.12 enables internal ingress policies by
  default; ServiceMonitor discovery alone does not authorize this traffic.
Storage canaries are temporary acceptance resources, not steady-state GitOps
workloads. Create them only for a guarded storage or upgrade test, verify both
StorageClasses and payload checksums, then remove their workload, claims, retained
PVs, and Longhorn backends after proving complete detach.
- Longhorn's native HTTPRoute selects `longhorn-frontend:80` through the K3s
  Gateway; only the real hostname value comes from private Git.

The previous Flux HelmRelease/HelmRepository files and unused HelmRelease schema
are removed. Argo CD is the only deployment controller for Longhorn.

| Wave | Dependency |
| ---: | --- |
| 1 | Child-Application health customization and dedicated Prometheus Operator CRD chart |
| 2 | Longhorn controller/CSI, encrypted `longhorn` class template and ServiceMonitor |
| 3 | OpenBao; its PVCs select `longhorn` |
| 4 | Vault Secrets Webhook |
| 5 | Cloudflare credential, issuer and wildcard certificate |
| 8 | Persistent Prometheus, Alertmanager and Grafana monitoring |

The CRD-only Application installs the dedicated pinned Prometheus Operator CRD
chart. Longhorn can create its ServiceMonitor in wave 2; the Prometheus operator
and storage-backed Prometheus start in wave 8.
Prometheus selects ServiceMonitors and PodMonitors across namespaces/releases,
including Longhorn's metrics Service. See
[`08-observability/kube-prometheus-stack/README.md`](../../08-observability/kube-prometheus-stack/README.md)
for the handoff from existing ephemeral monitoring.

## The normal `longhorn` class encrypts new volumes

New dynamically provisioned PVCs using **`storageClassName: longhorn`** get V1
LUKS/dm-crypt encryption, ext4, expansion, three replicas and `Retain`. There is no
second encrypted class. **`local-path` remains the cluster default** for PVCs
without a class; manually created UI volumes are outside this StorageClass rule.
Existing volumes are **not** converted, even though their class name is unchanged.

Longhorn 1.13.0 does not expose encryption/CSI key references in its Helm values.
The Application therefore uses `encrypted-storageclass-template.yaml` as its final Argo
source, intentionally overriding the chart's ConfigMap. Argo may report a
`RepeatedResourceWarning`; the last source is the desired owner. Do not deploy
with a standalone `helm upgrade -f values.yml`, because that omits the encrypted
override. `verify-longhorn-encryption.rb` checks the source order and template.
Update both values and override when changing replica count, filesystem or retention.

**Before merging source changes into the tracked `main` branch, stage the ownership
handoff for clusters that already synced the former `longhorn.io/Node` manifests.**
The current Application has automated pruning enabled, so removing those objects
from the branch-tracked source without first applying `Prune=false` to the live
tracked custom resources could delete Longhorn's active Node records. Perform
that separate, reviewed transition before merging and syncing this simplified
source; do not use the new core Node manifests as substitutes for the existing
Longhorn custom resources.

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
- StorageClass `longhorn-monitoring` is also non-default. It uses two replicas
  selected onto the `monitoring-storage` tags initialized from metadata-only
  Kubernetes Node annotations on OMV and Wyse. The Pi remains excluded from
  Prometheus metrics storage.
- UI Service: **ClusterIP**, because the shared Gateway provides LAN/VPN access.
- Pod Security `privileged` labels target the Longhorn namespace through Argo's
  `managedNamespaceMetadata` and `CreateNamespace=true`, not Application `spec.labels`.
- Manager requests: 100m CPU / 256Mi RAM; memory limit: 1Gi. Encrypted-volume
  discovery can transiently run a roughly 445Mi `cryptsetup` process inside the
  manager cgroup during startup, so the steady-state manager footprint alone is
  not a safe basis for this limit.
- The v1.13 global manager runs three replicas on distinct nodes with 100m CPU /
  128Mi RAM requests, 500m CPU / 512Mi RAM limits, and a PDB requiring two
  available replicas. The node-local manager upgrade retains the chart's native
  rollout strategy so ownership transfers follow the supported v1.13 sequence.
- Instance-manager CPU reservation uses Longhorn's native 12% V1/V2 default;
  measure total CSI/engine/replica overhead rather than treating the managers'
  requests as the whole storage budget.

Three nodes provide useful failure tolerance only when all volume replicas are
healthy on distinct nodes. Increase replica counts on **existing** volumes
separately; changing defaults affects only new volumes. Scaling OpenBao from one
Raft pod to three creates four additional PVCs (data and audit for each new pod);
neither pod placement nor Raft quorum is proven just by changing a value. Verify
disk capacity, resource budgets, CSI availability and Raft membership during rollout.
The current `createDefaultDiskLabeledNodes: "false"` preserves Longhorn's default
disk creation on joining nodes. Git annotates only OMV and Wyse before Longhorn
creates its custom Node resources; do not assume storage on other joining nodes
will remain excluded without changing that policy.

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
kubectl get nodes omv wyse5070 -o jsonpath='{range .items[*]}{.metadata.name}{"\\t"}{.metadata.annotations.node\\.longhorn\\.io/default-node-tags}{"\\n"}{end}'
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

Longhorn 1.12.0 fixed upstream bug #11510, which can leave a LUKS mapping and
V1 iSCSI session deadlocked after instance-manager loss. The cluster subsequently
moves to the current stable 1.13.0 release rather than stopping at the first fixed
minor. Before syncing this upgrade, require healthy replicas, current external
recovery evidence, no stale host sessions, Kubernetes 1.34 or newer, and
confirmation that no V2 backing-image migration applies. The deployment is
validated against K3s/Kubernetes 1.37.1. Existing V1 encrypted volumes and their
1.12.1 engine images remain in place during the control-plane upgrade. Retain that
engine image until every volume is separately stopped, detached, upgraded, attached,
and data-validated; encrypted live engine upgrade is not accepted. Longhorn does
not support downgrade after a successful upgrade; do not interrupt or force-delete
instance managers during rollout. The chart pre-upgrade hook remains disabled to
keep fresh bootstrap viable because it references a ServiceAccount created by the
same sync; run the equivalent target-version pre-upgrade command explicitly before
merging an upgrade.

If a fresh-install or upgrade test fails, stop subsequent syncs and diagnose; do
not blindly uninstall the chart, delete its namespace/CRDs, or remove the host data
path. Once volumes exist, recovery/rollback must preserve them and follow Longhorn's
supported upgrade/recovery procedures rather than an assumed safe version downgrade.
