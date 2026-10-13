# RAM optimization — 2026-10-13

Canonical plan/status record. Ticket: https://github.com/dmuiX/k3s-on-omv-infra/issues/35

## Authorization and scope

The earlier preparation commit was reverted and PR #36 closed without merging.
The user subsequently explicitly renewed the request to implement the three
measures. This revision applies only the reviewed monitoring/provider/cache
changes through their existing GitOps owners. No CRD, managed external resource,
Secret, PVC, PV, Longhorn volume, or retained TSDB/WAL data may be deleted.
The user separately approved two NAS-only profiling/cleanup restarts and then
explicitly requested the same procedure on Pi and Wyse through the described
short-lived privileged diagnostic Jobs. All three procedures are complete.
No further host maintenance or automatic recurring restart was enabled. No unproven process memory limit or
root-wide manual sync is part of this work.

## Fresh preflight

- Base: public `main` at `e4ae23d`; current database restructuring is preserved.
- NAS-local working set: K3s service 4.21 GiB, Kubernetes pod cgroups 5.59 GiB.
- Prometheus: 1,024,628 head series, 554,392 scraped API-server samples,
  413,804 post-relabel samples, three healthy API-server targets.
- Retained latency histogram: 40,220 active series; volume capacity: 18 series.
- 253 loaded rules, zero unhealthy rules. None of the six candidate metric
  families is referenced by the 37 live rulesets or 30 dashboard ConfigMaps.
- Nine PVC identities and twelve Longhorn volume identities/replica counts
  were captured privately for comparison. The different volume/PVC totals
  predate this revision and are not a cleanup target.
- A separate root/database rollout is retrying; the previous monitoring
  operation was terminated externally. Do not overwrite or abort operations.
  Changed Git revisions are serialized by the existing Argo controllers.
- All statements about current health must be refreshed before each mutation.
  Existing database/Grafana health failures are not caused or repaired by this
  RAM change; no credential contents have been accessed.

## Implementation

1. **Monitoring — activated and live-verified:** filter six unused, high-cardinality
   API server/etcd histogram families by metric name. Preserve existing chart
   bucket thinning, latency/SLO/availability/storage metrics, every rule/dashboard,
   retention, storage declarations, and historical metrics. A pinned old/new Helm
   comparison proves that only the API-server ServiceMonitor changes, ignoring
   the already-reviewed Grafana credential randomness/checksum.
2. **Provider/cache scope — activated and live-verified:** replace wildcard future
   MRD activation with the two database managed resource types declared in Git.
   Manage the existing default MRAP explicitly: Helm init only creates it once.
   Exclude unused Vault API groups/generated MRDs from Argo caches, preserving
   existing exclusions and visibility of every authored desired resource.
   Already active MRDs/CRDs stay registered; Active → Inactive is forbidden by
   Crossplane and would require a separate destructive migration.
3. **K3s diagnosis — completed on all three nodes:** the exact
   installed K3s source confirms delegated authentication/authorization for
   profiling. All three etcd members passed health checks before NAS activation.
   An immutable root-owned cleanup helper, serialized cleanup lease, health
   checks and systemd rollback timer were installed before the temporary
   profiling overlay. Profiling rejected unauthenticated access with HTTP 403.
   Sanitized heap/goroutine stack profiles were captured without requesting
   cmdline, environment, arbitrary memory or credential objects. The helper
   removed only its overlay and queued the second normal restart. Normal mode
   returns HTTP 404 for authenticated profiling. Pi/Wyse used sequential,
   digest-pinned, tokenless, bounded Jobs and the same owned cleanup helper,
   protected by an exclusive renewable Kubernetes maintenance Lease. Their
   unauthenticated profiling requests also returned 403. All profiling overlays
   are absent, all original/delayed cleanup timers/services are inactive, and
   all temporary diagnostic Jobs/Pods were removed with UID-bound deletion.
   The maintenance Lease is released; all three etcd members/nodes/API targets
   are healthy.

## Validation

Ten applicable checks pass on the refreshed base: bootstrap, cert-manager API
network policy, Application health (seven Lua fixtures), OpenBao PKI, ingress,
Longhorn encryption, Pi placement, RAM regression/baseline render comparison,
pinned upstream chart renders (including Crossplane), and PostgreSQL checks.
Private sibling-repository fixtures are not available and are not included.
MRAP/Argo ConfigMap server-side dry runs pass. The bootstrap-owned default
activation/exclusion fields require Argo's normal SSA ownership handoff; no
resource is force-replaced. Rendered Secret contents are never printed.

## Live results and limits

- PR #37 merged as `c5f60aa63f09dd4c532874024bc60044aca60306`.
  The changes were activated by the existing serialized Argo controllers;
  unrelated database/Grafana rollout failures were not terminated or repaired.
- Loaded API ServiceMonitor has both the original thinning and new filter.
  Post-relabel samples fell from 413,804 to 205,671 with all three targets up;
  subsequent lower counts also include counter/series resets from K3s restarts
  and must not be attributed solely to filtering.
- Exact reviewed Argo exclusions are loaded; controller reports 197 cached API
  resource types. The default MRAP contains exactly the two required types.
  The 253 original rules were healthy at the NAS gate; 264 current rules are
  healthy at final peer acceptance (other actors added monitoring concurrently).
  Retained latency/storage metric families remain present.
- The original nine PVCs/twelve volumes were preserved at NAS acceptance. The
  fresh peer-maintenance inventory contains twelve PVCs/fifteen volumes after
  the separate database rollout; all their UIDs/replica counts were preserved.
- NAS K3s working set fell from 4.21 GiB preflight to 1.87 GiB after the normal
  second restart. NAS process RSS is approximately 1.4 GiB versus about 3.7 GiB
  beforehand; goroutines decreased from ~24,600 to ~5,000.
- This is a measured short-term restart effect, not proof of durable remediation
  or a memory leak. Heap samples highlight OpenAPI/schema encoding, CRD/CEL
  validation and watch/cache allocations. Sampled bytes are not exact RSS.
- Wyse working set immediately before activation: 4.05 GiB; normal-mode
  acceptance: 1.83 GiB. RSS fell from ~3.62 to ~1.40 GiB; the captured fresh
  goroutine profile contains 4,355 goroutines (previous telemetry ~18,200).
- Pi initial working set: 2.23 GiB; normal-mode acceptance: 1.34 GiB. Its
  process PID had already changed before our activation, at which point working
  set was 1.33 GiB. Thus the initial-to-final reduction cannot be attributed
  solely to this procedure; the cause of that earlier restart remains unknown.
  Its captured fresh goroutine profile contains 4,399 goroutines (initial
  telemetry ~23,800). Both peers show similar OpenAPI/encoding/watch paths.
- Twenty regression tests pass across cleanup, atomic overlay publication,
  filesystem boundaries, exact health reply/timeout, wrong-target/peer refusal,
  timer-before-publication ordering, delayed cleanup and bounded tokenless Jobs.
  Actual immutable production entrypoints were exercised and accepted via real
  profiling/normal endpoints, PID/start identities, etcd health and protected
  storage checks—not merely Job/controller metadata.
- Two Job-reporting problems were reproduced: a 45-second deadline raced a
  successful exit on Wyse, and Pi's containerd restart lost an activation
  container's exit status (`Unknown`/255). No activation was blindly replayed.
  Job timeout was raised to 180 seconds; cleanup now uses a managed 15-second
  delay so its diagnostic container exits before the host runtime restarts.
  The updated Pi cleanup Job completed successfully before normal-host/endpoint
  acceptance. Direct activation can still lose runtime status, so independent
  host verification remains mandatory; a failed Job alone does not prove an
  unexecuted restart.

## Warm follow-up: existing kubelet profiling, no restart

The earlier assumption that profiling always needs a K3s restart was incomplete.
The existing authenticated node-proxy kubelet endpoint
`/api/v1/nodes/omv/proxy/debug/pprof/{heap,goroutine}` profiles the same embedded
Go process without enabling the separate supervisor profiling router. Both
routes were exercised successfully; labels were stripped before storage.
Future diagnosis must check this existing authorized interface first. The
supervisor endpoints remain disabled and all temporary overlays remain absent.

The retained job-labelled metrics cover only about four hours, not the entire
four-day request window. After taking scheduler/controller-manager/cloud-controller
leadership, NAS process RSS reached ~3.02 GiB and ~26,100 goroutines without a
further restart. Current holder/acquisition timestamps correlate with the rise;
this is not by itself a proof of either a leak or exclusive causation.

The actual warm goroutine profile reports 26,157 goroutines, including 1,062
reflector resync loops, 891 listener run loops, 891 listener pop loops, 667 watch
HTTP handlers and many gRPC callbacks. The warm heap's largest sampled stacks
include cached OpenAPI v2 JSON/protobuf encoding, OpenAPI v3, watch decoding and
gRPC buffers. Forced-GC HeapAlloc was 1,262,607,000 bytes versus HeapSys
2,989,031,424 bytes; sampled stack weights must not be confused with exact RSS.

Live telemetry reports 1,846 active API WATCH requests cluster-wide, including
533 for Vault API groups. Metadata/name output counts 389 CRDs, including 259
Vault-named CRDs, and 248 Vault MRDs. Argo exclusions do not suppress controller-
manager/garbage-collector or provider watches; the narrowed MRAP does not
retroactively deactivate existing Active MRDs. The structural next candidate
is reducing unused *registered* provider API types, not just hiding them in Argo.

Before any such migration: verify actual instance counts via fresh metadata-only
responses, inspect all current Git/composition references and package ownership,
keep required resource types/configurations, prove controller re-registration
behavior on a test deployment, and obtain approval for the exact deletion/migration
set. No CRD/MRD was deleted in this investigation. Acceptance must compare warm
profiles under equivalent leader roles and workload, plus sustained memory/watch
counts—not cold memory immediately after a restart.

## Live acceptance and rollback

- Use the same single-owner Argo path; pin each selected operation to the tested
  revision and never replace a currently active operation.
- Observe actual post-relabel sample reduction and freshly ingested retained
  metrics with all three API targets up and unchanged rule health.
- Verify the exact two-entry default MRAP and loaded Argo exclusion setting;
  verify required Git-managed resource types remain visible.
- Compare protected PVC/volume UIDs and replica counts, nodes, and working set.
  TSDB head series and RAM reclamation may lag a head-compaction cycle; never
  claim immediate RAM savings from only a current-query cardinality decrease.
- Roll back only this change through Git and the same owners. Never delete
  storage, credentials, CRDs, TSDB/WAL files, or unrelated rollout resources.
