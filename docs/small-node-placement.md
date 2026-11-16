# Small-node placement and bundled add-on adoption

This change implements issue #78 without changing PostgreSQL roles, volume
replication, node storage membership, or host memory reservations.

## Placement

Bootstrap already owns the capability `compute-class=standard` on `omv` and
`wyse5070`, and `compute-class=small` on `raspi4`. CoreDNS, Metrics Server and the
SMB **controller** select the standard capability. SMB node plugins and local
Longhorn components remain eligible on the Pi. Three Longhorn replicas per CSI
controller family use native hard pod anti-affinity; they are not volume replicas.
ztunnel retains its CPU request and limits, with a common 128Mi memory request.

**CSI maintenance gate:** Longhorn 1.13.0 detects an anti-affinity preset change
and deliberately deletes/recreates the affected CSI controller Deployments rather
than rolling them in place. Each affected controller family can have a temporary
zero-ready window. Pause provisioning, attach/detach, resizing and snapshot
management operations for the approved window. Existing volume engines, replicas
and local plugins are not the target, but continuing application I/O must still
be verified. Changing the preset back has the same controller-recreation risk.
Do not activate this value without explicitly approving that maintenance impact.

Moving workloads frees actual memory on the Pi, not necessarily cluster-wide.
Lowering requests only frees scheduler budget. Do not equate these results.

## Ownership and fresh bootstrap

K3s installs its bundled DNS and metrics resources during the base build. Base
acceptance still verifies DNS before Argo CD exists; neither component is disabled
in the server configuration. During GitOps bootstrap, the shared preparation tasks
put documented K3s `.skip` markers on **all servers** before applying the Infra
root. Existing resources are retained. Do not use K3s `--disable` for migration:
it uninstalls the bundled resources.

The pinned CoreDNS chart adopts the existing immutable Deployment selector and
`kube-dns` Service. It intentionally does **not** own the retained K3s CoreDNS
ConfigMap: its local Corefile, custom imports and dynamically maintained NodeHosts
must stay private and intact. The chart mounts both Corefile and NodeHosts and the
optional custom ConfigMap. No Service IP or local DNS data is authored in Infra.
Keep the ConfigMaps and Service; do not enable pruning or force resource replacement.

The pinned Metrics Server chart creates `metrics-server-native` alongside the
legacy workload. Its chart APIService is disabled. The separately authored wave-1
APIService changes the backend only after wave-0 chart resources become healthy.
The existing aggregated-API TLS policy is retained, and kubelet TLS verification
is not disabled. Bootstrap acceptance requires the new rollout, Available
APIService, correct backend, and fresh metrics for the exact node cohort before
scaling only the known legacy K3s Deployment to zero. Old DNS/metrics resources remain for
rollback; no DNS/metrics uninstall or deletion is part of the handover. This is
separate from Longhorn's native CSI controller recreation described above.

## Existing-cluster activation order

Publishing these auto-syncing Applications is an operational action. Use an
explicitly approved maintenance window and command sequence.

1. Record the existing DNS Service UID/IP, CoreDNS ConfigMap UID/content checksum,
   original workload/APIService specifications, database primary and volume health
   in **private operational artifacts**, never a public issue or commit.
2. On the reviewed Bootstrap revision run its bounded maintenance playbook with
   `system_addon_phase=prepare`. This writes only skip markers after preflight;
   it does not restart K3s or remove any running resources. Verify all servers.
3. Publish/reconcile the reviewed Infra revision. Ownership transfer requires
   server-side field ownership takeover, not deleting or recreating CoreDNS.
   Server dry-run with takeover must preserve the DNS Service UID/IP and Deployment
   selector. CoreDNS uses a surge Pod with zero unavailable during its rollout.
4. Require the two new/updated Applications healthy, SMB off the small node,
   Longhorn CSI spread across nodes after its separately approved controller
   recreation window, and the expected ztunnel request.
5. Run the same maintenance playbook with `system_addon_phase=accept`; only after
   its live metrics gates does it scale the legacy metrics Deployment to zero.
6. Verify DNS answers through the existing DNS Service from a workload, node/pod
   metrics, a freshly approved disposable storage lifecycle, healthy unchanged
   data replicas, unchanged database primary, and OpenBao 3/3 functional. Record
   actual working sets and memory requests separately.

When a node is unavailable, hard CSI anti-affinity intentionally leaves that
node's replica Pending rather than concentrating replicas on one remaining node.
Two healthy instances per controller family remain when one of three nodes fails.

## Rollback

Prepare and approve exact rollback commands before activation. Stop the affected
Argo Application from reconciling **before** restoring any old spec. Restore the
saved CoreDNS Deployment in place, keeping its existing Service and ConfigMaps.
For metrics, first scale the retained legacy Deployment back to one and wait for
its rollout; only then restore the previous APIService backend. Verify DNS and the
metrics API before considering removal of the skip markers. Removing skip markers
is a separate reviewed host change that hands reconciliation back to K3s.

For the remaining values, revert only the SMB controller selector, Longhorn CSI
anti-affinity preset and ztunnel memory request through GitOps. A ztunnel request
rollback may leave the third OpenBao Pod unschedulable, so recheck the memory budget.
Never restore database roles, change data replicas, delete volumes, disable local
CSI plugins, or restart the K3s cohort as part of this rollback.
