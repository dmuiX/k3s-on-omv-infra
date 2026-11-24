# Infrastructure workload contracts

## Public repository CI

`.github/workflows/bootstrap-order.yml` runs **Repository workload contracts** on
pull requests, main pushes and manual dispatch. It checks only this public repo;
there is no private checkout, repository token input, production kubeconfig or deployment.
A separate native-admission job uses only its owned network-isolated disposable API.

```sh
python -m pip install -r tests/requirements-ci.txt
python tests/ci_dependencies.py --cache /tmp/infra-ci-downloads \
  --charts /tmp/infra-ci-charts --bin /tmp/infra-ci-bin
export PATH="/tmp/infra-ci-bin:$PATH"
export PLATFORM_CHART_AUDIT=/tmp/infra-ci-charts
python tests/repo_suite.py --list
python tests/repo_suite.py
ruby -EUTF-8 tests/test_application_health.rb
```

Preparation is the only networked step: anonymous public HTTPS downloads of the
exact tools and all official remote charts in `ci-dependencies.json`. Every
archive is SHA-256 verified, including cache hits; chart extraction needs a fresh
destination. Delete/recreate only your own disposable render directory before a
second preparation, or choose a new path. Downloads may be reused, extracted
charts/executables are not trusted CI cache inputs. Redirect destinations are
checked before following them. The preparer rejects missing/extra chart pins.
Chart upgrades must update the coordinate and checksum together.

Tests use the real Helm/Kustomize engines with canonical public inline values
and **synthetic** site inputs under `fixtures/public-values`. The profile uses
`.example.invalid` names and Secret references, never production configuration
or credential values. The runner overrides private-repo environment paths,
discards inherited VSO source overrides and sets `KUBECONFIG=/dev/null` before
imports. Manifest tests validate the entire local Kustomize graph (including sibling bases, patches and generator files) before rendering; unreviewed loaders, private paths and repository escapes fail closed. Rendered Secret payloads and failed tool output are not printed.
This is input isolation for reviewed tests, **not a sandbox for arbitrary code**.

`suites.json` explicitly classifies whole modules or selected public methods.
New unclassified modules, overlapping/unknown selectors, empty suites and skipped
mandatory public checks fail. Integration-only modules are not imported by the
selector. Remaining methods in mixed modules are integration coverage, explicitly
reported as **NOT RUN by this runner**. Native timezone admission is covered by
the separate job below, not by this repository-only entrypoint. Full Python discovery is not the public CI entrypoint.
The CLI's own tests prove that an actual failing assertion exits nonzero and a
mandatory skip cannot become green. Helper tests also reject corrupted downloads,
unsafe redirects/archive paths, escaped source paths and broken monitor ports.

The workflow checks maintained CI helper formatting, Ruff and strict mypy. Ruby
application-health execution and retained integration-runner syntax checks run
even after a workload assertion fails, unless the workflow is cancelled.

## Workload-specific coverage

The scope was checked against a read-only deployment inventory. Names/versions
below describe public desired-state interfaces, **not current live health**.
Every Infra child must be explicitly classified by the render contract; a new
Application fails until its intended test scope is reviewed.

## Isolated PostgreSQL 18 rotation-grant check

From the repository root with Helm, Python with PyYAML, and Node/npm, render the
synthetic consumer fixtures and execute the generated migration in memory. PGlite
0.5.17 runs PostgreSQL 18.7 without a server connection. Only the temporary npm
install needs network access; use an existing install as the first argument to
`test_rotation_grants.mjs` to run fully offline.

```sh
(
  set -eu
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  npm install --prefix "$work" --ignore-scripts --no-audit --no-fund @electric-sql/pglite@0.5.17
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3 - > "$work/grants.sql" <<'PY'
import yaml
from test_postgres_consumer import baseline, render, rotation_sql, values

ordinary = baseline("radar")[0]
ordinary["metadata"]["name"] = "ordinary"
ordinary["spec"].update(name="ordinary", owner="ordinary")
migration = rotation_sql(
    render(values("grafana"), "kube-prometheus-stack").stdout,
    render(values("radar")).stdout,
    yaml.safe_dump(ordinary),
)
if migration.returncode:
    raise SystemExit(migration.stderr)
print(migration.stdout, end="")
PY
  node tests/test_rotation_grants.mjs "$work" "$work/grants.sql"
)
```

The check verifies repeat application, restricted membership options, unchanged
synthetic password, and no grant for the ordinary login. It also checks that
inherited access, access obtained by setting a different role, and a conflicting
grantor each abort the whole migration. This opt-in SQL check is not CI or native
CloudNativePG/OpenBao acceptance and must not be run against a live database.

### Native physical PostgreSQL restore

With Helm, Python/PyYAML and native PostgreSQL 18 executables on a non-root test
host, run the physical restore check:

```sh
python3 tests/rotation_grants_restore.py /path/to/postgresql-18/bin
```

This starts only disposable clusters under a private `/tmp` directory, disables
TCP, gives its subprocesses a scratch home and a minimal environment, and uses
synthetic test passwords. It takes a `pg_basebackup` of a database with one missing and one
incorrect grant, verifies it with `pg_verifybackup`, and boots the copied data
directory. It applies the generated migration twice, runs the exact rotation SQL
from both Helm renders over a SCRAM-authenticated connection as the restricted
manager, and verifies that both app roles
can log in with the rotated passwords and read their restored marker. This proves
the native SQL and physical restore behavior, not Argo CD execution or CloudNativePG,
Barman and OpenBao controller integration. CI does not invoke this opt-in check.

## Native timezone admission tests

The `timezone-admission` job executes all tests in `test_timezone_defaults.py`
against an owned, network-isolated API with a digest-pinned K3s image, two CPUs,
2 GiB RAM, readiness/terminal-state checks and exact-ID cleanup. It is independent
of the repository job, so a workload-policy failure cannot hide native results.
Locally, `test_timezone_defaults.py` has four offline guards and fourteen opt-in native API tests. These cover forced CronJob timezones (including actual Git-owned manifests), CREATE/UPDATE admission, literal/empty/valueFrom TZ replacement, envFrom overrides without source reads, unrelated env/reference preservation, init/native sidecars, Windows and new ephemeral containers, existing ephemeral immutability, idempotence and final rejection with mutation disabled. There are no per-application TZ exceptions. They never invoke the host's kubectl context. No test image is scheduled or executed by the agentless test API.

For a disposable test API, use the same pinned K3s version as the platform. Docker owns the process/health state; the server has a 900-second lifetime, no network access, no host mounts and no privileges. Its automatically created anonymous data volumes belong only to this test. Run from the Infra checkout in a dedicated shell:

```sh
image='rancher/k3s:v1.37.1-k3s1@sha256:ca7f37d993d82ef0dcdcfecb2e0e2618ea541dbaffc620c8cedebe01a82acd0d'
test_id=$(docker run -d --name "timezone-policy-test-$$" \
  --label purpose=timezone-policy-test --label owner=timezone-tests \
  --network none --cap-drop ALL --security-opt no-new-privileges \
  --memory 2g --cpus 2 --restart no \
  --health-cmd='/bin/kubectl get --raw=/readyz' --health-interval=5s \
  --health-start-period=15s --health-timeout=3s --health-retries=30 \
  --entrypoint /bin/sh "$image" -c \
  'exec timeout 900 /bin/k3s server --disable-agent --disable-cloud-controller --disable-helm-controller --disable-network-policy --disable=traefik,servicelb,local-storage,coredns,metrics-server --flannel-backend=none --egress-selector-mode=disabled --bind-address=127.0.0.1 --advertise-address=192.0.2.1 --node-ip=192.0.2.1 --node-name=timezone-policy-test')
trap 'docker stop -t 5 "$test_id" >/dev/null 2>&1; docker rm -v "$test_id" >/dev/null 2>&1' EXIT
# Repeat this read-only status check until it reports healthy, then run tests:
docker inspect --format '{{.State.Status}} {{.State.Health.Status}} {{.State.Pid}} {{.State.StartedAt}}' "$test_id"
TIMEZONE_TEST_CONTAINER="$test_id" PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s tests -p test_timezone_defaults.py -v
```

`192.0.2.1` is a documentation-only advertised address needed by the API's endpoint validator. Test clients use loopback inside the network-isolated container. The suite requires the dedicated purpose label, private PID namespace, unprivileged container, no bind mounts and `network=none` before installing any policy. It creates a dedicated tokenless test ServiceAccount, fixture namespaces, suspended synthetic CronJobs and unscheduled synthetic Pods/debug-container specifications. Its binding-disable tests run sequentially and restore bindings with verified mutation, not merely a successful apply. No production resources or credentials are used. Closing the dedicated shell cleans up that exact container and its anonymous volumes. The lifetime also bounds the process if the client disappears; Docker retains terminal status for recovery.

## Native RAM-limit contracts and API checks

`test_memory_limits.MemoryLimitsContractTest` is explicitly selected in the
public suite. Four actual-chart contracts check every allowlisted RAM cap,
shared RBAC init/Grafana sidecars, the coupled reloader/init profile, a typo
negative control and whole-document memory-only deltas. Requests, CPU caps,
HA/security and unrelated resources remain compared. See
[the profile and acceptance boundary](../docs/memory-limit-profile.md).

The existing owned API job explicitly runs only
`test_memory_limits.NativeMemoryLimitApiTest` (three tests), without private
inputs or Helm preparation in that job. It uses the guarded test API's actual
Kubernetes strategic-merge engine and dry-run Pod admission: paired init512Mi
is accepted, inheriting steady128Mi with request256Mi is rejected, generated
synthetic init image/args survive, and explicit requests/CPU caps stay unchanged.
No fixture image executes; no production context or actual Prometheus Operator
is used. Native merge/admission is NOT full operator/runtime acceptance.

## Per-Application coverage

| Infra child | Public contract / actual interface | Separate acceptance not proved here |
|---|---|---|
| `argocd-config` | Real chart, TLS mode, five monitor/service named-port targets; actual Argo CLI evaluates parent Application Lua health | Private routes, identity provider and real source reconciliation |
| `timezone-defaults` | Four offline contracts plus fourteen actual CREATE/UPDATE/deny admission tests in the isolated API job, including Git-owned CronJobs | Existing production Pods and host/OS timezone behavior |
| `istio-core` | All official component charts, native CNI/ztunnel DaemonSets, readiness, non-privileged CNI and consumed ztunnel resources | Node injection/traffic and actual ambient connectivity |
| `istio-ambient` | Public Gateway deployment/PDB overlays: three replicas, bounded rolling update, restricted seccomp | Private Gateway/Certificate binding and istiod-generated Deployment |
| `metrics-server` | Real official chart, two replicas with required host anti-affinity on standard nodes, native min-one PDB and no-surge rolling strategy, unchanged per-Pod requests/probes/security, one APIService with complete Service/RBAC wiring, wrong placement/HA-value negative controls | Existing K3s owner retirement, Argo adoption, actual two-host availability/failover, all-node metric freshness and HPA behavior |
| `prometheus-operator-crds` | Exact chart renders and safely parses the complete public CRD documents | Complete API-server/CRD schema validation |
| `barman-cloud` | Two real controller replicas, native account, health probes, CPU/RAM requests and limits | ObjectStore custody, archive/recovery and plugin sidecars |
| `cert-manager` | Three controller Deployments with two replicas, native reader account and supported probes | ACME challenge/DNS and issued certificate; native cainjector has no health endpoint |
| `cloudnative-pg` | Official operator chart, native account/monitor/PDB contracts | Reconciliation, failover and actual database operation |
| `crossplane` | Real controller chart plus provider activation/dependency contracts | Provider installation and actual OpenBao grants |
| `csi-driver-smb` | Native controller reader identity, real node DaemonSet, Secret-based StorageClass and retained volumes | Mount/provisioning with private server configuration |
| `k8up` | Two real controllers, enabled leader election and native monitor | Real repository access/backup/restore |
| `longhorn` | Real chart/PDB, three CSI replica settings, encrypted local storage class | Operator-generated CSI placement, attachment, storage health and data replicas |
| `vault-secrets-operator` | Native chart/shared transport, no default authentication, required-value transformation, isolated identity, exact-file Live policy source | Private policy tuples, authenticated scrape, OpenBao authentication, fresh reconciliation and consumer adoption |
| `provider-vault` | Pinned Provider, linked runtime config, exactly 16 explicitly activated resource types | Provider reconciliation and real OpenBao policy/identity changes |
| `provider-vault-connection` | Public resources render; dependency/credential ownership contracts | Private connection/custody and applied provider credentials |
| `platform-credentials` | Public resources render; credential ownership and isolated native reader accounts | Private role bindings, real values and rotations |
| `letsencrypt-dns01-credentials` | Public resources render; native cert-manager reader identity and waves | DNS provider credential and actual OpenBao policy |
| `letsencrypt-dns01-issuer` | Public Application reference/wave only; no fabricated issuer is rendered | Actual issuer resources are private Live configuration |
| `openbao` | Exact chart, three-server HA/PDB, rolling strategy, protected metrics listener/identity, named monitor port and network-policy invariants | Quorum, unseal, authentication, real metrics traffic and orphan retirement |
| `tekton-pipelines` | Pinned controller manifests, CRD schema hygiene and restricted `ci-builds` namespace | Controller rollout and actual build execution |
| `tekton-dashboard` | Read-only UI, scoped reader RBAC, synthetic OIDC/proxy/route agreement, private API policy source boundary, create-once credential custody | Gateway/TLS, allowed and denied OIDC sessions, NetworkPolicy and real log streaming |
| `longhorn-config` | Public trim-only RecurringJob and encrypted-discard DaemonSet; actual ConfigMap program/process/lease fixture tests; synthetic OIDC/proxy render | Real block devices/discard, private OIDC and CSI/node-plugin lifecycle |
| `openbao-backups` | Bounded exclusive CronJob/account and executable snapshot adapter transitions with fake endpoints | Real backup integrity, credentials, retention and recovery |
| `openbao-config` | Public render, prerequisite waves and credential ownership | Mount/auth/identity operations and real policies |
| `k3s-postgres` | Three-instance CNPG CR, required anti-affinity, supervised primary updates, encrypted local class, synchronous failover quorum, Barman plugin/ScheduledBackup; one complete raw ObjectStore with unchanged sizing and a required endpoint Secret reference; executable retention fixtures | [Opt-in pinned VSO parser](../docs/vso-required-values.md#focused-postgresql-endpoint-parser-rerun-no-sibling-repos), guarded Argo adoption, generated Pods, physical data health and backup/restore |
| `backup-validation` | Manual-only fixture seed Job and reconciled seed tool; suspended, tokenless `k8up-backup-validation` and exact restored marker; bounded exclusive PostgreSQL restore-validation CronJob/account, stable generated ConfigMap wired to the CronJob; actual import-safe store builder preserves local R2 references through the bridge | Private application chart consumers, safe selective sync on a suspended CronJob and actual database/application restores; the scheduled validator uses an explicit endpoint, not the env-only path |
| `kube-prometheus-stack` | Suspended TSDB restore test on monitoring nodes, verified archive and historical sample read; real chart, two-replica Prometheus with hard host anti-affinity, native alert rules, two-replica Grafana with secret refs and bounded rollout, disabled Alertmanager, independently staged Grafana policy source | Private policy tuples, gateway login, datasource queries and sidecar refresh on both replicas; real scrape delivery |

K3s-owned DNS/service-LB, private Apps and Bootstrap host ceremonies are not
owned or changed by this CI. Metrics Server's new GitOps chart is covered here;
the existing-cluster ownership handoff remains separate production acceptance. Generated Pods/controllers are checked at
the owning CR/chart/overlay seam; rendering a CR does not prove reconciliation.

## Private workspace integration (retained, not run publicly)

`tests/run-offline-tests.sh` remains the broader **cross-repository integration**
runner. It is not an autonomous public-repo test command: it needs coordinated
private Bootstrap/Live/Apps siblings, Ansible, Ruby, Helm/Argo/Kustomize and exact
chart caches. Inspect its inputs and use an authorized private workspace.

It retains Bootstrap custody/playbook/syntax coverage, real Live `$live` values
and raw source resolution, private Apps consumers, API target tuples, ownership
and Ruby mesh/source contracts. `CHART_CACHE_DIR` supplies the official Istio
charts to its legacy mesh checks. The public CI does not remove these tests or
claim their skips as acceptance. Any skipped required integration check remains
a release blocker in that validation lane.

## Limits / deployment gates

- No complete kubeconform/API-server schema pass is claimed. Helm values-schema
  validation where provided, safe YAML parsing and semantic contracts are not a
  substitute for all Kubernetes/custom-resource schemas.
- Isolated native timezone admission is exercised; this does not prove production
  admission or policy activation. No production policy is installed by CI.
- No runtime rollout, reachability, failover, storage, backup/restore, credential
  rotation or recovery acceptance occurs here. Ansible lint/private entrypoint
  validation belongs to Bootstrap/private integration, not this public workflow.
- A green public job is not deployment approval. Private integration and fresh
  live functional evidence remain separate required gates.
- CI-only rollback is a source revert of this PR; no production rollback command
  or cluster mutation is part of this change.
