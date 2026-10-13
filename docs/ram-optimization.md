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

## Read-only feasibility research: 2026-10-13

Current `origin/main` was refreshed to `16e69e0d89e79622aaf81cb6d5e4a8bfc75939d7`.
The later Radar OIDC change adds four required Vault types; the old assumption
that only the two database types are needed is no longer valid. Radar is healthy
and its four objects exist. Current Argo exclusions no longer exclude the whole
legacy identity group, but the live default MRAP still names only two types.

A complete 253-type Vault API inventory used strictly negotiated
`PartialObjectMetadataList` responses, with no full-payload fallback. Only
name/namespace/UID/count were retained; labels/annotations/spec/status/credential
fields were not persisted or printed. All 253 requests succeeded. Empty lists
returned `items: null`, handled by a regression-checked parser.

- 248 managed resource types: **242 empty candidates**, six nonempty retained.
- Empty candidates: 124 namespaced and 118 cluster-scoped types.
- Retain `SecretBackendConnection` (one instance), `SecretBackendStaticRole`
  (two), and `OidcClient`, `OidcKey`, `OidcProvider`, `OidcScope` (one each).
- Retain provider-configuration infrastructure: the existing `ProviderConfig`
  and seven `ProviderConfigUsage` references; do not include the other three
  configuration CRDs in the managed-type deletion set.
- No live Compositions/CompositionRevisions/XRDs were listed. Fresh current Git
  headers reference those six managed kinds. This is a point-in-time inventory,
  not authorization or a guarantee against concurrent future creation.

Exact source/lifecycle checks:

- Crossplane **v2.4.2** forbids Active → Inactive via its MRD CEL rule.
- Its package establisher preserves current Active state when desired is
  Inactive. Updating the activation list or upgrading alone is not a reset.
- The SafeStart-capable package creates missing MRDs as Inactive; narrow MRAP
  activation can then activate only the retained types. This reset/reconcile
  sequence must be demonstrated on an isolated test cluster before production.
- CRDs have both MRD and ProviderRevision owners. Deleting an MRD alone does not
  reliably remove its CRD; deleting only an Active CRD causes re-creation.
- Provider-vault **v4.0.4** uses crossplane-runtime **v2.2.0**. Its CRD gate
  releases startup callbacks only once and removes them afterward; setting a
  gate false does not stop already-started controllers. A targeted provider
  rollout after withdrawing unused CRDs is therefore part of the candidate
  migration, not a K3s restart. The provider's get/list/watch CRD permissions
  were verified, and its revision advertises SafeStart.

Proposed order, **not executed**: extend future activation to all six retained
kinds; test a single synthetic inactive-reset/CRD-withdrawal/provider-rollout
cycle plus automatic re-registration/rollback; freeze creation for the exact
unused set; recheck zero instances/current references; recreate only empty MRDs
Inactive under the pinned package; remove only their empty CRDs with UID-bound
preconditions; roll out only the provider; verify preserved seven instances,
configuration UIDs, OIDC/database behavior, watch counts and warm leader-equivalent
memory. Fresh source/ownership checks and explicit approval are required before
any destructive production step. Do not uninstall the whole provider.

Private inventory/candidate artifacts are retained locally. No CRD/MRD/resource
was deleted, no live activation/configuration changed and no restart occurred
in this research pass. Expected removable CRDs: 242 (389 → 147 if the migration
works as tested); no exact GiB savings are promised.

Sources: Crossplane `v2.4.2` `apis/apiextensions/v1alpha1/mrd_types.go`,
`internal/controller/pkg/revision/{reconciler,establisher}.go`,
`internal/controller/apiextensions/managed/{reconciler,crd}.go`;
provider-vault `v4.0.4` `cmd/provider/main.go`; crossplane-runtime `v2.2.0`
`pkg/{gate/gate,reconciler/customresourcesgate/reconciler}.go`.

## Dependency: bootstrap issue #46

User clarification identifies the parallel migration as
https://github.com/dmuiX/k3s-on-omv-bootstrap/issues/46, "Move steady-state
OpenBao configuration to Crossplane GitOps". Its current researched ownership
matrix adds twelve explicitly mapped future managed kinds beyond the six
already in use:

- `vault.vault.upbound.io`: `Mount`, `Policy`;
- `kv.vault.upbound.io`: `SecretBackendV2`;
- `auth.vault.upbound.io`: `Backend`;
- `password.vault.upbound.io`: `Policy`;
- `kubernetes.vault.upbound.io`: `AuthBackendRole`;
- `database.vault.upbound.io`: `SecretBackendRole`;
- `identity.vault.upbound.io`: `Group`, `OidcRole`;
- `pki.vault.upbound.io`: `SecretBackendRole`, `SecretBackendConfigUrls`,
  `SecretBackendCrlConfig`.

All twelve resolve to installed pinned MRDs and currently have zero instances
in the metadata inventory. **Zero today does not mean permanently unused.**
Reserve them in the coordination matrix; do not withdraw types belonging to an
active #46 stage. Other currently empty definitions may remain available but
Inactive, then be activated through Git only when the corresponding reviewed
stage needs them. Keep the complete pinned provider package rather than a
six-kind-only fork. The earlier 242-empty count is an inventory, not a final
approved destructive set.

Reactivation requires the MRAP/Helm future-activation list, matching Argo
visibility and health handling, CRD establishment/SafeStart acceptance, and #46's
security/adoption gates. Keep bootstrap trust root, provider's own policy/auth
role, ProviderConfig/ServiceAccount/package/runtime and secret custody outside
provider ownership. Reservation/registration is not authorization to expand the
provider's OpenBao privileges. Existing object adoption remains Observe-only,
Orphan and non-pruning until exact importer/no-rotation/drift/rollback evidence.

Potential newly created OIDC assignments or other later-selected types require
an additional reviewed mapping; unsupported imports, global destructive OIDC
reset, human enrollment and one-shot PKI key ceremonies are not implied by the
reservation. The test migration must prove reactivation of an additional reserved
kind after withdrawal, not only successful removal of an unused kind.

No live activation, privilege, registration, resource or restart was changed
while coordinating this dependency.

## Isolated real-component lifecycle proof: 2026-10-13

A disposable, agentless K3s API used a snapshot of the installed
`v1.37.1+k3s1` binary. Real Crossplane **v2.4.2** core/init and provider-vault
**v4.0.4** amd64 runtime/package were used, not mocked controllers. Immutable
image content was read/exported from the existing cache; the public upstream OCI
package was mirrored read-only inside the test network with SHA-256 verification.
All 248 MRDs were established; only synthetic Policy/Mount types were activated.

Boundaries: internal Docker network, no published ports, no privileged/host-PID/
host-network containers or added capabilities; no production data/credential/
kubeconfig mounts. Only code and newly generated test configuration/TLS were
mounted. API 1.5 GiB, Crossplane 640 MiB, provider 768 MiB, registry 96 MiB;
CPU and PID bounds, owner labels, immutable container IDs, persistent state and
independent systemd cleanup deadlines. Foreground control only, no AI workers.

Thirteen live checks passed:

1. The actual API rejects Active → Inactive.
2. Narrowing MRAP leaves an existing Active MRD active.
3. Deleting only the empty Mount CRD recreates it with a new UID.
4. Orphan deletion of only its MRD leaves the CRD in place.
5. Explicit reconciliation of the real pinned ProviderRevision recreates the
   missing MRD **Inactive**, because SafeStart and the narrowed MRAP apply.
6. Removing its still-empty CRD after that reset leaves it absent.
7. A protected synthetic Policy instance retains its original UID.
8. MRAP expansion reactivates the MRD/recreates the CRD and starts the real
   provider Mount controller without another provider restart.
9. Repeating empty reset/withdrawal does not unregister the started controller.
10. Its deleted-Mount informer emits retry errors after withdrawal.
11. One targeted test-provider restart removes that unused controller.
12. Final rollback through MRAP starts it again without an additional restart.
13. Both protected Policy instance **and** Policy CRD UIDs remain unchanged.

The runtime controller labels and informer behavior were verified independently
of resource existence. Initial/final controller sets exclude/include Mount as
expected. Guard regressions cover wrong ID/owner, non-internal network,
production client overrides, server-side apply, and API errors versus proven
NotFound (unknown resource aliases must never count as absence).

Setup-only issues were corrected before lifecycle acceptance: explicit internal
node/advertise IP for the route-less network; generated test kubeconfig ownership;
CLI boolean spelling; initial discovery refresh; named disposable TLS and DAC
readability with all capabilities dropped; exact `/cache/xpkg` directory mount;
bootstrap ServiceAccount; mirror the actual amd64 package rather than the
separate README/SBOM extensions artifact. Missing cache-directory creation caused
an opaque `io: read/write on closed pipe` via an ignored cache-store error in
crossplane-runtime **v2.4.2**; this was a test setup fault, not a production leak.
A safety deadline actually removed an early controller fixture and was reconciled
without replaying any production operation.

Final cleanup: all owner-bound containers, test volume, internal network and
active timers removed/stopped; generated test keys deleted; public code/proof
remains private locally. Independent metadata-only production recheck confirms
all seven original managed-instance UIDs, ProviderConfig and seven usage UIDs
still exist. All three production nodes remain Ready. Production configuration,
registrations, resources and services were not changed/restarted by this test.

This proves the registration/ownership/reactivation mechanism, **not** exact
production RAM savings or database/OIDC maintenance safety. The package-generated
Deployment was intentionally unscheduled (no test worker nodes); its real runtime
was exercised directly in Docker. Production requires exact-set approval, writer
coordination/#46 reservations, fresh zero-instance checks with race protection,
rollback source, preserved UID checks and functional database/OIDC acceptance
before/after the targeted provider rollout. No blanket 242-type deletion approved.

## Prepared coordinated batch (not executed)

The optional single-type production pilot was dropped. Current main
`c14d18afd7cdff9450405edff61135abbe7e0e32` was merged into the preparation branch
without losing its Radar/Argo/database changes. A new complete metadata-only
253-type inventory again has zero errors, six populated managed kinds/seven
instances, and protected ProviderConfig/seven usage references.

**Exact proposal: 229 empty managed API registrations**, listed one per line in
[`vault-registration-removal-candidates.txt`](vault-registration-removal-candidates.txt).
Retain six in-use types plus twelve #46 target types and, conservatively, the
conditional `OidcAssignment` API: **19 managed types total**, not only six.
The five provider-configuration CRDs are outside this deletion set. Conditional
reservation does not approve unsupported adoption or broaden provider ACLs.

`tools/vault_registration_plan.py` is an offline **PLAN_ONLY** planner, with no
Kubernetes client or execution option. It rejects partial/failed/stale inventory,
changed package/capabilities/ownership, unhealthy revision, live composition
ambiguity, duplicate/mismatched/wildcard identities, and retains nonempty,
Git-referenced or activation-matched types. Inputs and exact per-type CRD/MRD UIDs
are private; the candidate list is bound by plan SHA256
`7648c43df626e044aa2717673392837a3099570d4d46de45e0ae2097893e1415`.
Nineteen focused planner regressions pass; all 39 current Python tests pass.
The initial real-input failure identified ProviderRevision's actual `spec.image`
field; fixtures now match its three concrete health/active condition types.

Prepared Git policy expands Helm/default MRAP to all **six existing required**
DB/Radar OIDC kinds, with exact equality regression. The existing pinned monitoring
render/RAM regression passes. This correction is committed preparation, not live
activation of #46's future types and not permission to delete APIs.

A CREATE-only, fail-closed ValidatingAdmissionPolicy/binding is generated solely
for the exact 229 candidate plurals/groups/all served versions, leaving retained
APIs and UPDATE/DELETE untouched. The real production API accepted **server dry
run** of both objects; independent NotFound checks prove neither persisted.
Dry run validates schema/CEL, not enforcement. Activation must demonstrate a
fresh CREATE dry-run being denied by this exact policy before relying on it.

Approved execution must follow these stages (all pending):

1. Serialize with #46/Argo/package writers using a renewable maintenance lease;
   verify versions/UIDs/Git baseline and deploy the six-kind future policy through
   its existing GitOps owner. Keep provider trust-root/custody untouched.
2. Capture before-state/rollback and fresh warm leader/workload baseline. Verify
   existing DB/OIDC functionality and protected resource UIDs, PVCs/PVs and rules.
3. Install only the owned CREATE guard; verify its enforcement and inventory all
   candidates again. **Abort** on any new instance/reference/UID/package drift.
4. Delete only the exact empty MRDs with Orphan propagation and UID/resourceVersion
   preconditions; request the existing ProviderRevision's reconciliation. Wait for
   every replacement to be Inactive. Do not delete the Provider/Revision/package.
5. Recheck emptiness and guarded CRD ownership; delete only the listed empty CRDs
   with UID/resourceVersion preconditions. Journal per-type completion; never
   retry an unverified action after timeout or use wildcard deletion.
6. Execute **one** targeted provider Deployment rollout through the existing
   maintenance/GitOps owner, not K3s restarts. Verify rollout plus fresh functional
   DB/OIDC events, preserved UIDs and the reduced controller/watch set.
7. On failure, reactivate only journaled withdrawn types via a scoped temporary
   activation policy, restore their CRDs/controllers, remove only owned guard
   objects and release the lease after acceptance. Empty registration UIDs may
   change; all retained custom-resource/configuration UIDs must not.
8. Compare sustained warm leader-equivalent heap/RSS/goroutines/watches; do not
   promise a GiB figure or confuse a provider rollout with a K3s leak fix.

No guard, six-kind policy, deletion or production restart has been activated.
The final exact-set production action still requires explicit scope approval;
this prepared inventory will be refreshed rather than trusted after it ages.

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
