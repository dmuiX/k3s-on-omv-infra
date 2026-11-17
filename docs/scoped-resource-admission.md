# Scoped native admission resource defaults — BLOCKED / DO NOT ACTIVATE

This separate proposal uses Kubernetes `admissionregistration.k8s.io/v1`
MutatingAdmissionPolicy + Binding, no webhook/controller/fork. It is NOT a chart
resource override and must not be bundled silently into the native application
profile. **Merge activates automatic GitOps reconciliation and new-Pod mutation.
Keep draft until capacity, actual controller compatibility and lifecycle gates
are explicitly accepted.** No production admission write or Pod restart included.

## Exact scope / ownership

| Policy | Pod identity | Target | Missing request default |
|---|---|---|---|
| Longhorn V1 memory | Namespace longhorn; component=instance-manager; data-engine=v1; SA longhorn-service-account; controlling longhorn.io/v1beta2 InstanceManager with same name/nonempty UID; known node omv/wyse5070/raspi4 | instance-manager regular container only | RAM 448Mi on raspi4, 640Mi on omv/wyse5070; CPU untouched |
| K3s ServiceLB | Namespace kube-system; nonempty native service-name/namespace labels; SA svclb; controlling apps/v1 DaemonSet svclb-* with nonempty UID | lb-tcp-N regular containers only | CPU 5m, RAM 4Mi per container |

Only Pod CREATE; no resize/UPDATE, existing Pod, init/ephemeral container,
Longhorn V2, unrelated SA/owner/namespace/container or other kube-system workload
is modified. Explicit requests, including zero, and API-derived limit defaults
remain unchanged. Partial resource maps are extended, never replaced; no limits,
image, args, CPU guarantee, replica, storage or network settings are changed.
New Longhorn nodes require explicit profile review; unknown node names are not
silently assigned an unmeasured memory profile. Only current TCP ServiceLB
container families are covered, not a promise for future UDP/SCTP workloads.

Namespace/labels/owner references/SA fields constrain accidental blast radius,
**not authentication**: someone allowed to create privileged Pods in these
namespaces can spoof metadata. This is resource defaulting, not an authorization
or policy-enforcement replacement. Existing RBAC/Pod Security remains authoritative.
Policies fail closed for matching requests and are reinvocation-safe; errors can
block new storage/network Pods, so actual API and lifecycle testing matters.

[Longhorn v1.13.0 CPU comparison](https://github.com/longhorn/longhorn-manager/blob/v1.13.0/controller/controller_manager.go#L367-L376)
compares only CPU; [its setting sync](https://github.com/longhorn/longhorn-manager/blob/v1.13.0/controller/instance_manager_controller.go#L1031-L1045)
uses that comparator. This supports adding RAM without a CPU-setting mismatch,
but is NOT full running Longhorn-controller/storage recovery proof. Longhorn
pins instance managers with nodeName: do not assume a scheduler Pending state
will harmlessly protect an over-budget node; kubelet admission/recovery may fail.
K3s still owns its DaemonSet and Pods; mutation occurs only on Pod admission.

## Sizing evidence / limitations

Available 2026-11-16 seven-day working-set samples, not uninterrupted recovery
history: instance-manager P95 approximately 356Mi on Pi, 488/531Mi on standard
nodes (later fleet maximum 532.9Mi; peak 558.9Mi). Requests use roughly 20% rounded
headroom. Preserve existing native CPU (observed 480m Pi/Wyse, 1920m OMV), not a
new fixed CPU profile. Fresh per-node reload/rebuild/restore peaks remain a gate.

Refreshed ServiceLB normal P95/peak <=2.03/2.72Mi; other two TCP containers
P95 about0.31Mi, peaks <=0.56Mi. CPU P95 sampled near zero. 5m/4Mi is a
small utility reservation with margin, NOT a measured startup/failover upper
bound. Requests do not cap bursts and kernel forwarding CPU can be charged
outside these containers. Current layout: three TCP containers per node.

## Hard capacity blocker

The [request reductions](request-rightsizing.md) bring the native Pi profile
2204->2108Mi including helper and Barman/CSI/native placement. This proposal adds
448Mi IM +3*4Mi ServiceLB=460Mi: **2568Mi** before other placement/surge/unsupported
targets. The user's paired992Mi reserve activation was observed effective at
approximately **2337.66Mi allocatable**, with OpenBao3/3 Ready. The complete source
profile is still short **230.34Mi**, or **358.34Mi including128Mi spare**. Recovery
of existing Pods does not prove adoption or fit of future admission reservations.
These are placement-model projections, not physical-memory/runtime safety proof.

Do not lower measured requests merely to force green or cut daemon/eviction
protection. A separately approved capacity/placement/architecture decision and
fresh all-host accounting are mandatory; no storage membership/data-HA reduction
is proposed here. No simultaneous instance-manager restart or blanket Pod delete.

## Validation / acceptance boundary

Source contracts check the two scoped CREATE policies and canonical Application.
The existing digest-pinned, no-network, no-agent disposable K3s API job executes
native admission, alongside timezone policies: per-node RAM, partial/explicit/zero
and limit-derived requests, CPU/limits/extra resources/args/image preservation,
wrong namespace/owner/SA/labels/container and V2 exclusions, idempotence and init
exclusion. No test images run and no production credentials/endpoint are mounted.

This is admission E2E, **not actual Longhorn/K3s ServiceLB controller convergence,
volume recovery, CNI reachability or production adoption**. Before activation:
exact served API/plugin readiness; fresh per-node selector agreement; current
all-node reservations/physical/ancestor-cgroup headroom including Pending/init/
restartable sidecars/surge/failover; no conflicting LimitRange/defaults; storage
and backup health; isolated actual-controller compatibility; attended lifecycle
approval and serial recovery checks. Keep draft while these gates remain open.

## Disable / rollback runbook (human approved, NOT executed)

Automated prune is false. Removing/reverting source does **not** automatically
remove a live Binding; never claim source revert alone disables mutation.

1. Review and remove these two bindings from desired state; reconcile that source
   so self-heal will not recreate them. No Pod deletion is part of this operation.
2. With separate explicit production approval, remove only these live bindings:

   ```sh
   kubectl delete mutatingadmissionpolicybinding \
     default-longhorn-v1-instance-manager-memory default-k3s-servicelb-requests
   ```

3. Verify their absence and scoped representative server dry-runs after admission
   propagation. Existing Pods keep their admitted requests; disabling defaults
   does not shrink/restart them. Policy deletion/retirement can be reviewed later.
4. Any existing Pod resource/lifecycle rollback is another explicitly approved
   storage/network operation, never a blind delete or database switchover.
