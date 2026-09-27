# Longhorn: single-node bootstrap

## Structure and ordering

Folder prefix `02-` denotes Longhorn's controller deployment wave. Its UI
route is a separate wave-6 Application rendering the public
`charts/cluster-config/` template with the private hostname value; placing it
after certificate configuration avoids blocking Longhorn on TLS readiness. The
namespace and release name remain `longhorn`. HTTPS only becomes usable once
the wildcard certificate is ready.

- `app.yml`: pinned Longhorn Helm chart and reusable values from the public Git source.
- `values.yml`: single-node storage settings; no embedded Flux HelmRelease.
- The public route template selects `longhorn-frontend:80` through the K3s
  Gateway; only the real hostname value comes from private Git.

The previous Flux HelmRelease/HelmRepository files and unused HelmRelease schema
are removed. Argo CD is the only deployment controller for Longhorn.

| Wave | Dependency |
| ---: | --- |
| 1 | Child-Application health customization; monitoring chart, CRDs and operator |
| 2 | Longhorn controller/CSI and ServiceMonitor; cert-manager/K8up can run alongside |
| 3 | OpenBao, whose data and audit PVCs explicitly select `longhorn` |
| 4 | Vault Secrets Webhook |
| 5 | Cloudflare credential, issuer, wildcard certificate |
| 6 | Longhorn UI route rendered with the private hostname value |

Monitoring runs before Longhorn rather than adding another CRD-only Application
or deploying manually copied CRDs. Prometheus selects ServiceMonitors
and PodMonitors across namespaces/releases, including Longhorn's metrics Service.
Its data is ephemeral during bootstrap; persistent monitoring needs an explicit
revisit of the order so it cannot depend on storage that has not been installed.

## Single-node settings

- PVC/StorageClass replica count: **1** in the initial one-node phase.
- OpenBao initially runs **one Raft server pod** with separate data and audit
  PVCs; each Longhorn volume starts with one storage replica.
- UI-created volume defaults: **1** for each data engine (does not enable V2).
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

One node provides no node-failure tolerance. After three suitable nodes and
disks are ready, separately raise the Longhorn defaults to three for **new**
volumes, increase replica counts on **existing** OpenBao volumes, and verify
three healthy copies on distinct nodes. Scaling OpenBao from one Raft pod to
three creates four additional PVCs (data and audit for each new pod); neither
pod placement nor Raft quorum is proven just by changing a value. Revisit disk
capacity, resource budgets and CSI availability as part of that staged change.
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
   and Argo auto-prune/self-heal flags. There are no live Longhorn PVCs to migrate
   in the currently inspected cluster; recheck before any deployment.
4. Sync in a separately approved batch and verify controllers/CSI are ready and
   the `longhorn` StorageClass exists with one replica and `Retain`.

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
a separate gate and node failover cannot be proven on this one-node setup.

Longhorn's chart renders the StorageClass specification inside a ConfigMap for
its components to create. Seeing that ConfigMap or an Argo `Healthy` status does
not by itself prove working CSI provisioning/mounts.

If a fresh-install test fails, stop subsequent syncs and diagnose; do not blindly
uninstall the chart, delete its namespace/CRDs, or remove the host data path. Once
volumes exist, recovery/rollback must preserve them and follow Longhorn's supported
upgrade/recovery procedures rather than an assumed safe version downgrade.
