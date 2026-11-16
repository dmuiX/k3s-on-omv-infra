# Small-node workload tuning

This change addresses issue #78 through three existing Helm values. CoreDNS and
Metrics Server remain K3s-managed and unchanged. There is no add-on ownership
handover, new system Application, APIService change, K3s skip marker or Bootstrap
migration. Their existing memory requests are already modest.

## Scope

- Place the SMB **controller** on `compute-class=standard` nodes (`omv` and
  `wyse5070`). Bootstrap already owns these capability labels. SMB node plugins
  remain eligible on `raspi4`.
- Use native hard pod anti-affinity for Longhorn's four CSI controller families.
  Keep three replicas per family: one on each eligible node, four rather than
  twelve CSI controller Pods on the Pi after convergence. Local Longhorn
  components, storage-node membership and volume/data replication are unchanged.
- Reduce ztunnel's common memory request from 512Mi to 128Mi. CPU request and
  limits remain unchanged. This frees scheduler budget, not actual RAM.

No PostgreSQL role/configuration, DNS/metrics configuration, host reservation,
credential, volume or data-replica change is included. The earlier DNS/metrics
migration was removed from the draft before any live activation.

## Expected effect and boundaries

In the investigation snapshot, moving the SMB controller removes approximately
67 MiB of actual Pi working set and 80 MiB of its memory requests. Redistributing
CSI controllers may additionally reduce actual Pi usage; this is not guaranteed
and must be measured under comparable workload and leader roles.

Based on the recorded effective requests, ztunnel alone frees enough scheduler
budget for the pending 256Mi OpenBao server: approximately 166 MiB remains after
it is admitted. Moving the SMB controller raises this to approximately 246 MiB.
These are scheduling calculations, not a guarantee of sufficient physical RAM or
complete reservations: several Longhorn components still have no memory request.
The measured follow-up targets and their blocking budget calculation are defined
in [the resource request profile](resource-request-profile.md). They are a proposal,
not active values: the natively configurable additions alone exceed current Pi
headroom by about 346Mi. Do not apply them as part of this rollout without first
resolving that budget and the unsupported component overrides.

CoreDNS and Metrics Server stay on their existing K3s configuration, each with a
70Mi memory request. Their placement and consumption are not claimed as savings.

## CSI maintenance gate

Longhorn 1.13.0 detects a change of the anti-affinity preset and deliberately
**deletes/recreates the affected CSI controller Deployments** instead of rolling
them in place. Each controller family can have a temporary zero-ready window.
Provisioning, attach/detach, expansion and snapshot management may be interrupted.
Volume engines, data replicas and node-local plugins are not the target, but
continuing application I/O must still be verified. Reverting the preset has the
same controller-recreation risk. Explicitly approve the maintenance impact and
exact deployment commands before activation; a draft PR is not that approval.

After convergence, losing one of three nodes leaves one replica per family
Pending rather than concentrating replicas on the remaining nodes. The other two
replicas remain; this steady-state behavior does not describe preset migration.

## Activation and acceptance

1. Review current GitOps values, target-node headroom, node capability labels,
   volume health, existing database primary and before/after memory baselines.
2. Approve the CSI interruption window and rollback sequence before merging the
   auto-syncing Infra change. Do not run any previous DNS/metrics migration steps.
3. Require SMB controller placement on a standard node, three Ready replicas per
   CSI family distributed across three hosts, healthy local plugins and unchanged
   data-replica counts. Check the ztunnel request/limits on all nodes.
4. Verify the existing DNS and metrics APIs still work, a freshly approved
   disposable storage lifecycle, unchanged database primary and volume health,
   and OpenBao 3/3 with a fresh functional check.
5. Report actual RAM relief separately from scheduler-request headroom. Full
   cross-repository tests and production E2E are not implied by static contracts.

## Rollback

Revert only the three Helm value changes through the existing GitOps ownership.
The Longhorn preset rollback needs the same approved CSI interruption window.
Restoring ztunnel's larger request can leave the third OpenBao Pod unschedulable;
recheck the budget first. Do not change DNS/metrics ownership, database roles,
data replicas or local storage plugins, and do not restart the K3s cohort.
