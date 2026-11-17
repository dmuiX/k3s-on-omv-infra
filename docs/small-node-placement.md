# Small-node workload tuning

This addresses issue #78 through existing Helm settings and a paired Bootstrap
resource/budget change. CoreDNS remains K3s-managed and unchanged. The subsequent
[Metrics Server handoff](metrics-server-gitops.md) separately introduces an official
chart on standard nodes with paired K3s configuration. No skip marker or runtime
Deployment patch is used; existing clusters need an attended owner transition.

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
- Reduce ztunnel memory request512Mi ->64Mi (previous target128Mi), CPU200m ->150m;
  retain CPU limits; [separate RAM sizing](memory-limit-profile.md) caps ztunnel
  at256Mi without changing these requests. [Measured follow-up](request-rightsizing.md) also reduces
  OpenBao server requests256Mi ->224Mi, retaining3 replicas and512Mi limits.
- GitOps owns Barman ObjectStore sidecar requests 250m/192Mi and unchanged
  250m/256Mi limits through a resource-only SSA manifest in the existing postgres
  Application. Endpoint and credential custody remain Bootstrap/OpenBao-owned.
- Paired Bootstrap: candidate Pi kubeReserved 1536Mi -> 992Mi, unchanged system
  and eviction reserves. No SQL, credentials or primary role is changed.

## Budget and remaining gaps

The arithmetic below predates the Metrics Server handoff. Its later placement
removes another 70Mi of Pi requests only after approved activation; this does not
make the complete future database/Barman profile fit automatically.

[The measured resource profile](resource-request-profile.md) records P95, peaks,
native support and limitations. With the original 1794Mi allocatable memory, the
native profile including OpenBao and discard-helper needs2108Mi and cannot fit.
The paired992Mi reserve gives about2338Mi allocatable and **230Mi request headroom**
after these reductions. It was observed effective with OpenBao3/3 Ready. The
previous2204Mi profile left134Mi; the earlier1024Mi candidate then omitted the
64Mi helper and missed the128Mi gate. These are historical selection reasons,
not a request to change the now-effective992Mi reserve. A previous960Mi candidate
failed the service-memory gate at~851Mi working set and remains outside scope. Recheck against fresh admitted Pods including init sidecars. Current
CPU request projections fit the observed node capacities as well; recalculate
before actual deployment.

This is not230Mi physical RAM newly available. Moving workloads changes Pi usage;
requests change scheduler accounting. V1 instance-manager, engine/helper and
UI/driver resource gaps remain explicit. The existing IM/ServiceLB admission
profile still reaches2568Mi, exceeding allocatable by230Mi before extra spare;
see scoped-resource-admission.md. No data-HA reduction is hidden in this change.

## Ordered activation, not automatic approval

1. Review/approve exact commands and separate Pi-service, CSI-node-plugin and
   eventual database-Pod impacts. A controller-interruption acknowledgement alone
   does not approve these additional operations. Do not merge Infra first. If
   Infra is already merged, inspect the root operation's pinned revision and
   child state: Pi recovery can release the old operation and then automatically
   start newer CSI/ObjectStore changes. Approve that continuation or explicitly
   arrange an operator hold before restarting; no automatic hold is assumed.
2. Use the ordinary reviewed Bootstrap `ansible/deploy.py` run. It recognizes an
   accepted base without reinstalling it and automatically reconciles managed Pi
   memory after base validation, before DNS/GitOps/OpenBao. There is no separate
   Pi memory maintenance command or rollout flag. An already converged setting is
   a no-op; file-only drift is repaired without restarting or requiring platform
   applications to exist. A necessary effective change requests explicit restart
   confirmation within the normal run, then checks whole-service working set
   <=864Mi, host MemAvailable >=1024Mi, peers/database/volumes and physical capacity
   for its conservative2204Mi guard (reduced source now2108Mi) plus128Mi and unchanged
   reserves before writing.
   Writers are serialized and observations refreshed under the lock. It applies
   a frozen canonical file, restarts only Pi K3s if needed, and verifies live
   config, a fresh lease, >=2332Mi allocatable, OpenBao/helper recovery and unchanged
   primary. Failure restores the prior file if backed up and stops without blind
   restart retries; restoring a file is not proof of running rollback. Check mode
   never locks, writes or restarts. Failed convergence never authorizes base reset.
   Independently verify the actual Pod/parent cgroup limit and no new OOM kills;
   effective-config/allocatable metadata alone is not runtime-memory acceptance.
3. Require all nodes, etcd/storage and OpenBao healthy before reconciling the Infra
   changes. ztunnel must converge before resource headroom is consumed. Verify SMB
   placement, CSI requests and distribution, healthy local plugins and unchanged
   data-replica counts. Do not directly patch operator-owned CSI Deployments.
4. Reconcile the resource-only ObjectStore manifest through the existing
   `k3s-postgres` Application, not a Barman maintenance playbook. Before activation,
   check projected CPU/RAM on **every** affected host, retaining at least 128Mi RAM
   and 100m CPU request headroom. Argo's client-side apply migration is disabled;
   SSA owns only sidecar resources, not endpoint/credential configuration. The
   object is protected against prune/delete. Do not replace or client-side-apply
   the partial manifest. No credential ceremony or forced switchover is run.
5. Inspect actual database sidecars: an updated ObjectStore is desired state, not
   proof that existing Pods use new requests. If CNPG requires rolling Pods or a
   supervised primary operation, obtain separate approval and use its supported
   procedure. Until then report the sidecar change as pending activation, not done.
6. Verify DNS/metrics unchanged, a fresh approved disposable storage lifecycle,
   database replication/backups, unchanged primary/data-replica counts, and OpenBao
   3/3 plus a fresh functional check. Compare actual RAM and requests separately.

The complete K3s service recently used about 851Mi working set. Historical service
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
