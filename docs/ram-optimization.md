# RAM optimization — 2026-10-13

Canonical plan/status record. Ticket: https://github.com/dmuiX/k3s-on-omv-infra/issues/35

## Authorization and scope

The earlier preparation commit was reverted and PR #36 closed without merging.
The user subsequently explicitly renewed the request to implement the three
measures. This revision applies only the reviewed monitoring/provider/cache
changes through their existing GitOps owners. No CRD, managed external resource,
Secret, PVC, PV, Longhorn volume, or retained TSDB/WAL data may be deleted.
The user separately approved two NAS-only profiling/cleanup restarts and later
conditionally requested comparison/profiling on the other two nodes. NAS
maintenance is complete; changing either other host still requires a usable,
explicitly agreed maintenance access path. No unproven process memory limit or
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
3. **K3s diagnosis — NAS completed, other nodes pending access gate:** the exact
   installed K3s source confirms delegated authentication/authorization for
   profiling. All three etcd members passed health checks before NAS activation.
   An immutable root-owned cleanup helper, serialized cleanup lease, health
   checks and systemd rollback timer were installed before the temporary
   profiling overlay. Profiling rejected unauthenticated access with HTTP 403.
   Sanitized heap/goroutine stack profiles were captured without requesting
   cmdline, environment, arbitrary memory or credential objects. The helper
   removed only its overlay and queued the second normal restart. Normal mode
   returns HTTP 404 for authenticated profiling; timer is inactive/unloaded,
   no cleanup job remains, and all three nodes/API targets are healthy.

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
  All 253 loaded rules remain healthy; retained latency/storage metrics exist.
- All nine protected PVC UIDs and twelve volume UIDs/replica counts remain intact.
- NAS K3s working set fell from 4.21 GiB preflight to 1.87 GiB after the normal
  second restart. NAS process RSS is approximately 1.4 GiB versus about 3.7 GiB
  beforehand; goroutines decreased from ~24,600 to ~5,000.
- This is a measured short-term restart effect, not proof of durable remediation
  or a memory leak. Heap samples highlight OpenAPI/schema encoding, CRD/CEL
  validation and watch/cache allocations. Sampled bytes are not exact RSS.
- Existing scraped peer data: Wyse RSS ~3.56 GiB / ~18,200 goroutines; Pi RSS
  ~1.96 GiB / ~23,800 goroutines. Direct SSH is denied, and a full Pi metrics
  request timed out; peer comparison therefore uses already-ingested metrics.
  No host configuration or restart was performed on either peer.
- Eight cleanup-helper regression tests pass, including transport timeout,
  exact health reply, busy lease, symlink/content/permissions rejection,
  no-quorum deferral, and exactly-once restart behavior. Its actual root-owned
  production systemd entrypoint also completed successfully, followed by real
  service/endpoint/quorum/storage verification.

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
