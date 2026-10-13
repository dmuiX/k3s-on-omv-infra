# RAM optimization — 2026-10-13

Canonical plan/status record. Ticket: https://github.com/dmuiX/k3s-on-omv-infra/issues/35

## Authorization and scope

The earlier preparation commit was reverted and PR #36 closed without merging.
The user subsequently explicitly renewed the request to implement the three
measures. This revision applies only the reviewed monitoring/provider/cache
changes through their existing GitOps owners. No CRD, managed external resource,
Secret, PVC, PV, Longhorn volume, or retained TSDB/WAL data may be deleted.
K3s profiling activation/restart requires its own verified maintenance gate.
No unproven process memory limit or root-wide manual sync is part of this work.

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

1. **Monitoring — tested, pending activation:** filter six unused, high-cardinality
   API server/etcd histogram families by metric name. Preserve existing chart
   bucket thinning, latency/SLO/availability/storage metrics, every rule/dashboard,
   retention, storage declarations, and historical metrics. A pinned old/new Helm
   comparison proves that only the API-server ServiceMonitor changes, ignoring
   the already-reviewed Grafana credential randomness/checksum.
2. **Provider/cache scope — tested, pending activation:** replace wildcard future
   MRD activation with the two database managed resource types declared in Git.
   Manage the existing default MRAP explicitly: Helm init only creates it once.
   Exclude unused Vault API groups/generated MRDs from Argo caches, preserving
   existing exclusions and visibility of every authored desired resource.
   Already active MRDs/CRDs stay registered; Active → Inactive is forbidden by
   Crossplane and would require a separate destructive migration.
3. **K3s diagnosis — maintenance gate pending:** process/Go metrics are collected;
   profiling is disabled. Determine authenticated profiling routing from the
   exact installed K3s source before enabling it. Confirm etcd quorum, node
   health, sole-node restart scope, rollback and absence of overlapping work.
   Allocation attribution/memory-leak claims require an actual heap profile.

## Validation

Ten applicable checks pass on the refreshed base: bootstrap, cert-manager API
network policy, Application health (seven Lua fixtures), OpenBao PKI, ingress,
Longhorn encryption, Pi placement, RAM regression/baseline render comparison,
pinned upstream chart renders (including Crossplane), and PostgreSQL checks.
Private sibling-repository fixtures are not available and are not included.
MRAP/Argo ConfigMap server-side dry runs pass. The bootstrap-owned default
activation/exclusion fields require Argo's normal SSA ownership handoff; no
resource is force-replaced. Rendered Secret contents are never printed.

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
