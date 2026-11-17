# Consumption-based resource request targets

**Native request targets are authored in the paired PRs, not deployed.**
CSI/global-manager requests use the supported Longhorn chart fields; Barman
sidecar resources are owned by the resource-only GitOps ObjectStore manifest.
Argo/operator reconciliation owns Kubernetes objects. Only the host-level Pi
kubelet reserve converges through the normal idempotent Bootstrap deployment.
Existing database Pods may need a separately approved operator rollout. Source
changes and tests alone are not deployment or approval.
The paired Bootstrap Pi reserve candidate must be effective before activating the
native profile. Components without a native override remain planning targets.

## Measurement and method

Source: available Prometheus samples in the seven-day window queried on
2026-11-16. Memory is container working set in MiB; CPU is five-minute CPU rate in
millicores. For shared defaults, use the highest observed per-series P95 across
the three nodes and round with roughly 20% headroom/minimum useful CPU reservation.
P95 values are computed per observed container series, then the highest is used;
short-lived historical series and role changes can bias these values. They are
not a seven-day uninterrupted steady-state load test or a weighted fleet P95.

Check the observed peak separately rather than turning every request into a peak
reservation. Requests do not cap bursts. No memory/CPU limit is lowered by this
proposal. Unseen restore/rebuild/failover load remains an acceptance requirement.

## Natively configurable request targets

The four CSI rows apply **per replica**, not to the entire Deployment. The three
node-plugin rows apply per container on each node. A shared default allows a
controller leader or backup responsibility to move without a new profile.

| Component | Memory P95 | Observed memory peak | CPU P95 | Proposed CPU request | Proposed memory request |
|---|---:|---:|---:|---:|---:|
| CSI attacher | 62.2 | 62.3 | 2.5 | 10m | 80Mi |
| CSI provisioner | 64.2 | 64.4 | 8.4 | 10m | 80Mi |
| CSI resizer | 61.4 | 61.5 | 1.9 | 10m | 80Mi |
| CSI snapshotter | 60.1 | 60.2 | 6.5 | 10m | 80Mi |
| Longhorn CSI node plugin | 37.4 | 305.1 | 94.4 | 125m | 48Mi |
| CSI node-driver registrar | 33.2 | 33.3 | 0.1 | 5m | 48Mi |
| CSI liveness probe | 34.2 | 35.3 | 2.2 | 5m | 48Mi |
| Longhorn global manager | 67.5 | 67.9 | 3.8 | 10m | 96Mi |
| Barman **instance sidecar** | 168.9 | 220.4 | 230.0 | 250m | 192Mi |

The seven CSI rows and global-manager row are now in the Infra Application.
The Barman row is in the paired Bootstrap contract; it is not a plugin-controller
resource change and does not force database Pod restarts or a switchover.

The CSI plugin's rare 305Mi peak is deliberately not its request; retain burst
capacity and inspect it if it recurs. Barman's proposed CPU request stays at its
existing 250m CPU limit rather than requesting more than the current cap. Its
192Mi memory request leaves the existing 256Mi limit unchanged. The Barman plugin
**controller** already has 50m/96Mi requests and is a different workload.

Native ownership/configuration paths:

- Longhorn chart `defaultSettings.systemManagedCSIComponentsResourceLimits` accepts
  JSON Kubernetes `ResourceRequirements`, including requests, for the seven CSI
  components in the first seven rows. A component name absent from its native
  typed structure must not be invented. Resource changes restart affected CSI
  components and need their own explicit rollout-impact approval.
- The global manager has a native chart `resources` block and does not currently
  add Pi requests because it is not on the Pi. Target-node headroom still needs
  checking. The driver deployer and UI templates in this chart do **not** render
  a native `resources` override; their targets are listed separately below.
- Barman instance sidecar resources come from the ObjectStore's
  `instanceSidecarConfiguration.resources`, **not** the plugin controller chart's
  `resources`. The existing postgres Application now reconciles a resource-only
  `ObjectStore` SSA manifest. Bootstrap continues first creation and verifies only
  protected configuration; it seeds initial resources from that same public GitOps
  profile, never hardcodes or reapplies them on resume. Client-side apply migration
  is disabled at the Application level, server-side diff preserves omitted fields,
  and prune/delete protection retains the shared object. Endpoint/credential custody
  is unchanged; no extra maintenance playbook or credential ceremony is required.

## Measured targets without a native override in this version

| Component | Memory P95 | Observed memory peak | Defined target | Status |
|---|---:|---:|---|---|
| Longhorn driver deployer | 20.0 | 20.2 | 5m / 32Mi | Chart template has no native resources override |
| Longhorn UI | 4.8 | 8.2 | 5m / 16Mi | Chart template has no native resources override |
| Driver deployer wait init container | 9.5 | 9.5 | 5m / 16Mi | No dedicated native resources override found |
| V1 instance manager on raspi4 | 355.8 | 376.4 | 448Mi; retain its existing CPU request | No native memory-request override found |
| V1 instance manager on omv | 531.3 | 551.0 | 640Mi; retain its existing CPU request | Same limitation |
| V1 instance manager on wyse5070 | 487.6 | 508.4 | 640Mi; retain its existing CPU request | Same limitation |
| Engine-image daemon | 29.7 | 37.1 | 50m / 48Mi | No native resource override found |
| Manager image pre-pull helper | 2.9 | 2.9 | 1m / 8Mi | No dedicated native resource override found |

These are planning targets, not implemented reservations. Longhorn 1.13's V1
instance-manager spec exposes image, node, type and data-engine settings, not a
resource template. Its CPU guarantee is configurable, but that is not a RAM
request. V2 memory/hugepage settings must not be repurposed for the V1 engine.
The CSI resource map does not cover the instance manager or engine-image daemon.
No ad-hoc Pod patches, new admission machinery, or storage-owner fork is included.

## Current measured follow-up

[The native reductions](request-rightsizing.md) lower the current native Pi target
2204->2108Mi, preserving limits/HA. With effective992Mi kubeReserved, allocatable
is~2337.664Mi and native spare~229.664Mi. Including the existing IM/ServiceLB
admission source still reaches2568Mi and does NOT fit (230.336Mi deficit before
128Mi spare). No further reserve/HA reduction is made. The budget below is the
historical issue78 selection arithmetic, not the current reduced target.

## Historical Pi budget gate and paired reserve (before request reductions)

Fresh read-only effective request calculation includes restartable init sidecars,
ordinary init-container maxima and Pod overhead. Terminated Pods are excluded.
All figures below are MiB, rounded only for display.

| Budget item | MiB |
|---|---:|
| Current allocatable Pi memory | 1,793.66 |
| Current effective Pod requests | 1,756 |
| Existing PR plus the third OpenBao server: 1756 - 384 - 80 + 256 | 1,548 |
| Already Pending encrypted-discard helper (not in the scheduled baseline) | +64 |
| Remaining request headroom | 181.66 |
| Four remaining CSI controller replicas at 80Mi | +320 |
| Three node-plugin containers at 48Mi | +144 |
| Barman sidecar 64Mi -> 192Mi | +128 |
| Total with these natively configurable targets, including both Pending Pods | 2,204 |
| **Deficit against unchanged allocatable memory** | **410.34** |
| Paired Pi kubeReserved candidate: 1536Mi -> 992Mi | +544 |
| Allocatable with that candidate and unchanged system/eviction reserves | 2,337.66 |
| **Native profile headroom after paired activation** | **133.66** |
| Additional currently unsupported Pi targets: instance manager + engine + helper | +504 |
| Total if all of the defined Pi targets were reservable | 2,708 |
| Full-target deficit against the original reserve | 914.34 |
| **Full-target deficit even with the paired candidate** | **370.34** |

This does **not** mean an extra 914Mi physical RAM is immediately consumed: the
processes already run. It means their proposed scheduling guarantees do not fit
the current reservation model. Do not force requests below the measured normal
load simply to make the arithmetic pass. Do not present the intermediate 182Mi
request headroom as complete coverage for these previously unreserved consumers.

The earlier 1024Mi candidate omitted the Pending 64Mi helper and leaves only
101.66Mi request headroom, below the unchanged 128Mi gate. A 960Mi candidate would
fit requests, but a fresh whole-service working set of about 851Mi exceeds its
832Mi service gate. The 992Mi candidate preserves both gates: at most 864Mi live
service working set and at least 128Mi request headroom. It is not a blind request
reduction or removal of daemon protection.

The normal paired Bootstrap deployment automatically reconciles this managed
host setting after accepted-base validation and before GitOps. No separate memory
maintenance invocation is required. Already converged configuration is a no-op;
file-only drift does not restart K3s. A necessary effective change requests explicit
confirmation inside the normal run, serializes writers and rechecks under the lock.
It checks physical capacity for the complete profile and reserves before writing,
requires at least 128Mi spare inside the 992Mi candidate and at least 1Gi host
MemAvailable, checks both remaining local API/etcd readiness gates, then verifies
effective config, a fresh worker lease, >=2332Mi allocatable, OpenBao/helper recovery
and unchanged DB primary. The installer/reset is never called on accepted resume. System reserve and all eviction protections stay unchanged. Full
historical service working-set peaks remain unavailable. This is a tight guarded
candidate, not proof that all future recovery spikes fit it. It changes scheduler
accounting, not a hard process-memory limit or physical RAM consumption.

The native profile fits this candidate; the complete profile still does not.
Do not claim all missing requests are solved: unsupported components and the
remaining 370Mi full-target deficit remain open. No storage-role or data-HA change
is included. Inspect actual Pods after operator rollout, recalculate fresh
memory/CPU headroom and complete the production tests before acceptance.

## Sources

- [Longhorn 1.13 CSI resource-map schema](https://github.com/longhorn/longhorn-manager/blob/v1.13.0/types/setting.go#L2491-L2499)
- [Longhorn 1.13 instance-manager spec](https://github.com/longhorn/longhorn-manager/blob/v1.13.0/k8s/pkg/apis/longhorn/v1beta2/instancemanager.go#L203-L215)
- [Barman 0.15.1 sidecar resource ownership](https://github.com/cloudnative-pg/plugin-barman-cloud/blob/v0.15.1/internal/cnpgi/operator/lifecycle_resources.go#L47-L67)
