# Pi scheduling and memory headroom

## Verified incident

OpenBao has two ready members; `openbao-0` has been Pending since 2026-11-16 13:43 UTC. Required hostname anti-affinity leaves only `raspi4`, where memory requests do not fit. The Pi's encrypted-discard DaemonSet member is Pending for the same reason. Consequently Infra waits for OpenBao, and `longhorn-config` waits for its DaemonSet; the trim RecurringJob update remains unapplied. The timezone policies are independently Synced/Healthy and live admission probes succeeded. This is not a timezone admission failure.

Read-only evidence:

- Pi physical capacity: `3868328Ki` (~3777Mi); kubeReserved `1536Mi`, systemReserved `256Mi`, hard memory eviction `192Mi` => ~1793Mi schedulable memory.
- The Pod cgroup is also capped at ~1985Mi. Its cumulative counters included eight OOM kills; their exact times are not known. Host MemAvailable was ~1630Mi, so free physical RAM does not imply the Pod cgroup has equivalent headroom.
- Current scheduled requests are conservatively rounded to 1760Mi, including PostgreSQL's backup sidecar and the old 512Mi ztunnel request. The missing OpenBao member needs 256Mi and the missing helper 64Mi.
- ztunnel's 24-hour measured working-set peaks were ~15Mi on the Pi and at most ~32Mi across the nodes. Its old request was 512Mi on every node.
- K3s working set was ~796Mi and RSS ~581Mi. Its service cgroup's ~1383Mi peak includes cache; lowering the scheduling reservation does not impose a daemon memory limit. No claim about an unmeasured historical RSS peak is made.

## Minimal coordinated changes

1. Infra `01-bootstrap/istio/app.yml`: ztunnel memory **request 512Mi -> 128Mi**, retaining CPU request 200m, CPU limit 1 and memory limit 1Gi. Official Istio chart/version and native readiness/zero-unavailable/one-surge rollout remain unchanged.
2. Bootstrap `ansible/roles/k3s_server/files/raspi4-kubelet-memory.conf`: Pi kubeReserved **1536Mi -> 1024Mi**. OS reservation and every eviction threshold/grace period remain unchanged. NodeAllocatable enforcement is not disabled.

The new schedulable budget is ~2305Mi; the aggregate Pod cgroup budget is ~2497Mi. Even before the old ztunnel disappears, 1760 + 256 + 64 + 128 = 2208Mi of conservative transition requests fit. After replacement, modeled steady requests are 1760 - 512 + 128 + 256 + 64 = 1696Mi. Lowering the ztunnel request alone is not the entire fix: with the old node budget, a zero-unavailable surge can be blocked before it replaces the old 512Mi Pod, and request changes do not raise the Pod cgroup's hard ceiling.

Requests are not measured usage or peak guarantees. LUKS attach/refresh can have substantial transient KDF demand; the helper's host-memory checks remain in force, and cgroup/OOM counters must be watched during live acceptance. No key was read, no cipher operation executed and no helper/storage memory limit weakened for this change.

## Tests and limits

The native kubelet budget regression failed with the old reserve. The exact official Istio `ztunnel` 1.31.2 chart render regression failed with the old request, then passed with 128Mi while preserving limits and the rollout strategy. Chart archive SHA-256: `683c7623f0ce650687e16d1ab481dee605a1158ac86e98732bd8cd21ec0bd2e9`.

Static tests prove configuration/request arithmetic, not safe live peak usage. Merge is not evidence that either Pending Pod became ready. Existing baseline full-suite failures are reported separately in the PRs. No live rollout was executed while preparing these changes.

## Activation — separate approval required

Scope: one Istio DaemonSet resource-request update plus one native kubelet file and one K3s service restart on **raspi4 only**. No host reboot, PVC replacement, database parameter change, HA reduction, forced volume detach, key operation or backup/trim Job is part of the operator procedure.

1. Verify all nodes and etcd endpoints are healthy, PostgreSQL peers stream, the two current OpenBao members are ready, attached volumes are healthy, and no other maintenance/writer is active. Record readiness, Pod identities, OOM counters and live memory/cgroup budgets. Coordinate the storage helper's normal activity; do not blindly repeat authenticated operations after an ambiguous failure.
2. Merge the reviewed Infra and Bootstrap changes only after approval. Expect Argo to roll ztunnel with maxSurge 1/maxUnavailable 0. Its Pi replacement may remain Pending until the node budget is activated; do not delete the existing ztunnel or weaken availability rules to force progress.
3. With separately authorized administrative access, install only the reviewed `raspi4-kubelet-memory.conf` as root-owned mode 0600 at `/var/lib/rancher/k3s/agent/etc/kubelet.conf.d/20-pi-memory.conf`, then restart only the Pi's K3s service. The existing full `host-k3s.yml` cohort build is NOT the memory-update path. Root/sudo access was not available in the diagnostic SSH session; no credentials were requested or bypassed.
4. Verify the effective config and increased allocatable/Pod-cgroup budgets, Node Ready and no new OOM kills. Require OpenBao 3/3 ready and unsealed, discard helper 3/3 ready, normal PostgreSQL streaming/roles and healthy attached volumes. Read only allowed health metadata; never dump credentials/environments. A restarted K3s process alone is not acceptance.
5. Verify Argo Infra/OpenBao/Longhorn/istio-core converge and the declared `35 18 * * *` trim schedule is finally reconciled. Confirm explicit Berlin scheduling without manually starting a trim or backup. Existing running application Pods are not restarted merely to update timezone.

On any new OOM, quorum/replication loss, storage error, prolonged node outage or unexpected policy denial, stop further mutations and gather scoped diagnostics. Do not restart other nodes, force-remove devices, reduce replicas or repeatedly retry KDF operations.

## Rollback

Revert the reviewed source changes and coordinate Argo/node recovery with explicit approval. Restore the previous native drop-in only after checking actual Pod-cgroup usage and absence of a memory-intensive storage operation; shrinking a busy cgroup can itself cause OOM. Reverting the file needs another controlled Pi K3s restart, not a host reboot. Reverting the ztunnel request to 512Mi may reintroduce the scheduling/surge deadlock, so it is not a blind automatic rollback. Preserve all PVCs, keys, affinity and HA counts.
