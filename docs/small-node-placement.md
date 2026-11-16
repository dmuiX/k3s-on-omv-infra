# Small-node workload tuning

This addresses issue #78 through existing Helm settings and a paired Bootstrap
resource/budget change. CoreDNS and Metrics Server remain K3s-managed and unchanged:
no new system Application, APIService, skip marker or add-on handover is included.

## Scope

- Place the SMB **controller** on `compute-class=standard` (`omv`/`wyse5070`).
  Bootstrap already owns these labels. SMB node plugins remain eligible on Pi.
- Use hard host anti-affinity for four Longhorn CSI controller families, keeping
  three replicas per family. After convergence the Pi has four instead of twelve
  controller Pods. Local plugins and data replication remain unchanged.
- Give each CSI controller 10m/80Mi requests; node CSI plugin 125m/48Mi;
  registrar/liveness each 5m/48Mi; global manager 10m/96Mi. Use the native resource
  setting and preserve existing unbounded burst limits, rather than inventing a
  V1 instance-manager override. These targets come from measured consumption.
- Reduce ztunnel memory request 512Mi -> 128Mi; keep CPU request and limits.
- Paired Bootstrap: candidate Pi kubeReserved 1536Mi -> 1024Mi, unchanged system
  and eviction reserves, and ObjectStore Barman sidecar 250m/192Mi requests with
  unchanged 250m/256Mi limits. No SQL, credentials or primary role is changed.

## Budget and remaining gaps

[The measured resource profile](resource-request-profile.md) records P95, peaks,
native support and limitations. With the original 1794Mi allocatable memory, the
native profile plus OpenBao needs 2140Mi and cannot fit. The paired candidate gives
about 2306Mi allocatable and **166Mi request headroom**, verified against a fresh
admitted-Pod snapshot including init sidecars. Current CPU request projections fit
the observed node capacities as well; recalculate before actual deployment.

This is not 166Mi physical RAM newly available. Moving workloads changes Pi usage;
requests change scheduler accounting. V1 instance-manager, engine/helper and
UI/driver resource gaps remain explicit. Their full proposed profile still exceeds
the new Pi budget by about 338Mi. No data-HA reduction is hidden in this change.

## Ordered activation, not automatic approval

1. Review/approve exact commands and separate Pi-service, CSI-node-plugin and
   eventual database-Pod impacts. A controller-interruption acknowledgement alone
   does not approve these additional operations. Do not merge Infra first.
2. From the reviewed Bootstrap revision run only bounded `configure-pi-memory`
   maintenance. It checks complete current K3s service working set <=896Mi and host
   MemAvailable >=1024Mi, peers, database/volume health, then installs the canonical
   file and restarts only Pi K3s if needed. It verifies live config, a new lease,
   >=2268Mi actual allocatable memory and unchanged primary. On failure it restores
   the prior file when backed up, stops and does not blindly restart again. File
   restoration is not a guarantee that the running configuration was reverted.
3. Require all nodes, etcd/storage and OpenBao healthy before reconciling the Infra
   changes. ztunnel must converge before resource headroom is consumed. Verify SMB
   placement, CSI requests and distribution, healthy local plugins and unchanged
   data-replica counts. Do not directly patch operator-owned CSI Deployments.
4. Run only bounded `configure-barman-resources` maintenance from the paired
   Bootstrap revision. Its fresh effective-request check requires at least 128Mi
   remaining after the sidecar increase. An atomic UID/resourceVersion/resource
   test replaces only the requests map, preserving the exact rest of the ObjectStore
   spec. No credential ceremony, forced Pod restart or switchover is run.
5. Inspect actual database sidecars: an updated ObjectStore is desired state, not
   proof that existing Pods use new requests. If CNPG requires rolling Pods or a
   supervised primary operation, obtain separate approval and use its supported
   procedure. Until then report the sidecar change as pending activation, not done.
6. Verify DNS/metrics unchanged, a fresh approved disposable storage lifecycle,
   database replication/backups, unchanged primary/data-replica counts, and OpenBao
   3/3 plus a fresh functional check. Compare actual RAM and requests separately.

The complete K3s service recently used about 781Mi working set. Historical service
peaks/recovery load are not fully covered; the live gate is not a long-term
capacity guarantee. Full CI and production E2E remain required.

## CSI lifecycle / rollback

Longhorn 1.13.0 **deletes/recreates controller Deployments on an anti-affinity
preset change**. Each family can have a temporary zero-ready window, interrupting
provisioning, attach/detach, expansion or snapshots. Resource-setting changes also
restart affected CSI components, including local plugins. Volume data is not the
deletion target; application I/O still needs testing. A preset rollback has the
same risk. A later single-node loss leaves two replicas per family available and
one Pending; this is the converged state, not the preset-migration behavior.

Prepare exact rollback commands and inspect the actual state before executing
any. Revert only owned chart values through GitOps. Restoring the larger Pi reserve
before removing added workload requests can make the Pod budget fail again.
Changing Barman desired state does not roll old/new sidecars back instantly.
Never infer successful runtime rollback from a restored file or a job exit code;
never force database roles, delete data replicas or restart the whole K3s cohort.
