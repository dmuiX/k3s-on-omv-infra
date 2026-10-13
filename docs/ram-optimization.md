# RAM optimization — 2026-10-13

Canonical status and verification record for the current optimization.
Tracking ticket: https://github.com/dmuiX/k3s-on-omv-infra/issues/35

## Baseline and constraints

- NAS-local Kubernetes working set: about 9.7 GiB (K3s service ~4.4 GiB,
  pod cgroups ~5.3 GiB). These are measured allocations, not requests.
- Prometheus TSDB: approximately 939,000 head series before changes.
- 389 registered CRDs, including 253 Vault provider API types; 248 active MRDs.
- Metadata-only enumeration of all 253 Vault types found one ProviderConfig
  and no managed external resource instances. The desired Git configuration
  still declares database connections and static roles for subsequent rollout.
- 37 live PrometheusRules and 30 dashboard ConfigMaps do not reference the
  six filtered metric families. Pinned chart rendering must confirm this.
- CRDs are not standalone processes. Their actual API server memory cost has
  not been attributed by a heap profile. Schemas/endpoints/caches and provider
  watches create overhead; Prometheus histograms multiply time-series count.
- Nine PVCs and nine Longhorn volumes were recorded at implementation preflight.
  No storage object, external resource, Secret, or etcd configuration may change.
- K3s pprof is disabled (`/debug/pprof/goroutine` returns 404). A heap profile
  requires a separately reviewed, controlled enable/restart; metrics alone do
  not establish an allocation cause or a memory leak.

## Plan and progress

1. **Monitoring — prepared and tested, not deployed:** filter six unreferenced
   API server/etcd histogram families at ingestion; preserve the pinned chart's
   existing bucket thinning, all rules/dashboards, storage/availability/SLO
   metrics, retention, PVCs, and historical TSDB data.
2. **Provider scope — prepared and tested, not deployed:** replace wildcard
   future activation with the two declared database managed types. Reconcile
   the existing default MRAP explicitly: Helm's init container only creates it
   once and will not update an existing wildcard policy. Exclude unused Vault
   API groups and generated MRDs from Argo's cache while preserving existing
   exclusions and every authored Kubernetes resource. Do not delete already
   active MRDs: Crossplane permits only Inactive → Active transitions.
3. **K3s profile — blocked on controlled activation:** verified about 3.5 GiB
   private process RAM, 2.7 GiB allocated Go heap, and ~24,500 goroutines.
   No process restart or unproven memory limit has been applied.

## Current deployment blockers

A concurrent root-wide Argo operation at revision
`7c8ec5e62de41423d8107c9fc454fcb8bd09ba5b` finished during verification.
Monitoring remains in an active sync waiting on Grafana. Do not overwrite,
terminate, or mutate resources inside that operation. Keep this change on a
separate branch/PR until the overlapping writer has finished and a fresh
preflight has been captured. No production RAM reduction is claimed yet.

The K3s profile additionally requires approval for a controlled profiling
activation/restart; no pprof endpoint has been exposed and no host service
configuration has been changed.

## Verification so far

- New RAM regression reproduced the missing policy on the baseline and passes
  after the patch, including an old/new pinned monitoring render comparison.
- Seven repository checks pass: bootstrap, cert-manager API egress, Application
  health (seven Lua fixtures), OpenBao PKI, ingress, Longhorn encryption, and Pi
  placement. Pinned chart renders (including Crossplane) and PostgreSQL checks
  also pass. Private/sibling-repository integration checks need their external
  fixtures and are not included in this acceptance result.
- Server-side dry runs succeed for the MRAP and Argo ConfigMap. Their two fields
  have bootstrap-created managers (`crossplane` / `kubectl-create`); Argo's
  standard SSA force-conflict handoff is required for these exact reviewed
  fields. No CRD or storage object is force-replaced.
- Pre-deployment Prometheus: three healthy API targets, 516,237 scraped samples,
  389,943 post-relabel samples, 253 loaded rules with zero unhealthy rules,
  35,080 retained API latency histogram series, and 24 volume-capacity series.
- All nine protected baseline PVC UIDs remained present at the last preflight.

## Gates and rollback

- New regression test must fail on baseline and pass after the patch.
- Run repository checks and pinned Helm renders; no rendered Secret output.
- Verify that the monitoring render changes only the API server ServiceMonitor
  (apart from ignored chart-generated credential randomness).
- Review GitOps ownership and apply only reviewed resources, not a root-wide
  sync of unrelated degraded applications.
- Live verification: loaded Prometheus filter, successful API-server scrapes,
  newly ingested required metrics, unchanged rule health, lower post-relabel
  sample count, selective default MRAP, no authored Argo resource exclusions,
  unchanged PVC/volume identities and replica counts, and three Ready nodes.
- Head-series/RAM reclamation may lag by a TSDB head-compaction cycle. A drop
  in current query series is not proof of immediately freed head memory.
- Roll back only these Git changes/reviewed resources. Never force-replace a
  ServiceMonitor, CRD, PVC, PV, or volume, and never delete TSDB/WAL files.
