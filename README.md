# K3s on OMV — reusable infrastructure base

This repository contains reusable Argo CD Applications for the initial K3s
deployment on a three-node OMV/K3s cluster. It retains Helm templates for
HTTPRoutes, DNS-01 certificates, OpenBao backups and manual restores in
[`charts/cluster-config/`](charts/cluster-config/), but no *real* hostnames,
ACME email, backup account identifiers or destination. The chart defaults use
`example.invalid`; its default `component: none` deploys nothing. Argo CD renders
version-pinned upstream charts with reviewed Git values; generated chart output
is not committed. Review public files and history before each push.

## Ownership and rollout

K3s owns Traefik, its Gateway and GatewayClass; the host `HelmChartConfig`
lives in a separate bootstrap repository. This repo does not install Traefik or
External-DNS. It uses Argo CD (already installed) for these applications:

```text
01-bootstrap/
├── argocd-bootstrap/       child-Application health customization
├── argocd-server-config/   existing Argo CD server backend configuration
└── monitoring-crds/        monitoring CRDs only (no workloads/PVCs)
02-controllers/
├── cert-manager/           certificate controller
├── k8up/                   backup controller
└── longhorn/               three-replica storage across the cluster
03-core/
├── kube-prometheus-stack/  persistent monitoring on Longhorn
└── openbao/                secret store
04-secrets/
├── openbao-access-config/  Git-managed webhook ACL, initial Job and recurring CronJob
└── vault-secrets-webhook/  admission-time secret injection
05-platform/
├── public-certificates/    wildcard certificate/issuers after secrets
├── openbao-pki/            mandatory internal PKI phase, staged inactive until ceremony
├── argocd/
├── grafana/
├── longhorn/
└── openbao/                early UI routes, usable when wildcard TLS becomes ready
06-data/
├── openbao-backups/        OpenBao backup schedule
├── postgresql/             staged central Crunchy PGO HA platform
└── redis/                  dormant Redis operator preparation (`application.yml`)
```

The first directory level is the parent sync wave; the second keeps component
ownership separate. Applications in one wave are independent and may start in
any order. A real dependency belongs in a later wave, not in an alphabetic
component name. Wave 7 is reserved for future applications and is intentionally
absent until one is implemented. The wave-1 CRD-only Application
renders only the monitoring CRDs before wave-2 controllers emit ServiceMonitors;
the full monitoring stack (Grafana, Prometheus, Alertmanager and operator) starts in
wave 3, after Longhorn, with explicit encrypted Longhorn PVCs. Longhorn also
precedes OpenBao PVCs; the secrets webhook and access-config Job follow OpenBao.
Wildcard certificate issuance and UI routes share wave 5 after OpenBao-backed
Cloudflare custody and webhook activation; routes may reconcile first but become
usable only when the Gateway certificate is ready. The backup schedule remains
separately gated until its OpenBao credentials exist. Argo CD's health customization
shares wave 1 with other Applications: **seed and verify it separately before a
first root sync**. Changing the existing Argo CD backend to HTTP also requires a
controlled `argocd-server` restart; plan that before enabling its external route.

`infra.yml` is the **one** Argo CD root for the application inventory.
The mandatory [`05-platform/openbao-pki/`](05-platform/openbao-pki/workload/README.md) platform phase is
staged rather than optional: the root include names its `application.yml`
explicitly, while the default root exclusion keeps it inactive until the
external-root ceremony and guarded installation have passed. Its workload source
is already pinned to an immutable commit. After those gates pass, the GitOps
bootstrap activates the pinned Application by promoting the root configuration;
do not register it independently or remove the safety gate ad hoc. The same
default exclusion gates the implemented `06-data/postgresql/app.yml`; the guarded
PKI installation activates both mandatory platform phases, and PostgreSQL then
runs as a Wave-6 acceptance target. OpenBao PKI adds no Certificate consumers. Upstream
controllers are Argo multi-source Applications: one version-pinned Helm/OCI chart
plus values from this Git repository. Route, certificate and backup Applications render the local
`charts/cluster-config` chart with private values from the live repository.
Authored Kustomize resources remain in Git, but rendered upstream chart output
is deliberately not vendored. The encrypted Longhorn StorageClass ConfigMap is
an explicit final Argo source overriding the chart resource that Longhorn
reconciles. Backends run in waves 1–3. Certificate issuance and UI routes follow
OpenBao/webhook together in wave 5, so routes are created at the earliest safe
platform stage and become reachable as soon as wildcard TLS is ready. Backup
configuration and PostgreSQL remain wave-6 data concerns.

The local chart installs no controller and defaults to `component: none`.
A rendered route does not prove working HTTPS: check Certificate Ready and the
K3s Gateway listener.

Argo CD needs read-only private Git access **before** the first root sync.
This cannot depend on OpenBao, which is installed by that sync: seed the Git
credential and child-Application health customization in a separately approved
bootstrap. Secrets such as Cloudflare tokens and backup passwords live
in OpenBao, not Git. The public templates use non-secret `vault:` references;
the private values file contains identifiers, not credentials.
The root and child Applications' automation and pruning settings require review before deployment.
The Grafana subchart creates its initial random administrator Secret in-cluster.
Argo ignores only the generated `admin-user` and `admin-password` data fields so
subsequent Helm renders do not rotate them. The values are never committed or
logged; inspect or rotate them only through an explicitly approved secret-access
procedure.

The `raspi4` node carries `CriticalAddonsOnly=true:NoSchedule`. Infra
controllers, OpenBao reconciliation and PostgreSQL explicitly tolerate that
taint. `kube-prometheus-stack` is the deliberate exception; even its node
exporter excludes `raspi4` to reserve the Pi's limited capacity.

### OpenBao backup credential admission

The public repo now contains a dedicated ServiceAccount, initial Job and
recurring CronJob for Git-managed webhook and human-admin ACLs. The local
bootstrap script can enable `userpass/` and interactively create a personal
user without putting a password in Git; MFA enrollment stays manual. **They are not deployed.**
Before enabling the new `openbao-access-config` Argo Application, run its
reviewed one-time local bootstrap script as described in
[`04-secrets/openbao-access-config/CONFIG-IAC-DESIGN.md`](04-secrets/openbao-access-config/CONFIG-IAC-DESIGN.md). Thereafter
edit `04-secrets/openbao-access-config/workload/policies/vault-secrets-webhook-read.hcl`
for webhook ACLs, `workload/policies/human-admin.hcl` for the human policy,
and `04-secrets/openbao-access-config/workload/config.json` for the webhook role
and mount references (KV v2 and userpass are verified, not recreated by the loop). Its intentional `kv/data/*` grant is broad:
anyone permitted to create and read a webhook-selected Kubernetes Secret may
request any KV v2 value under `kv/`.

Before enabling the K8up Schedule, create the `k8up-repo-password` and
`r2-credentials` entries in OpenBao's KV v2 engine at UI paths
`kv/k8up-repo-password` and `kv/r2-credentials`. They need the fields
`password`, and `access-key-id` plus `secret-access-key`, respectively.
Enter credential values only in the OpenBao UI, never in Git, commands or logs.

The existing webhook role has only the Cloudflare read policy. The bootstrap
attaches the stable `vault-secrets-webhook-read` ACL name to that role once;
the GitOps loop updates the policy **and** reconciles its declared role
settings in `config.json`.
Until bootstrapped, OpenBao still returns 403 on the backup paths, so Argo
cannot create the two backup Secrets and K8up remains in
`CreateContainerConfigError`.

After the policy and entries are ready, allow `argocd/openbao-config` to
reconcile through the reviewed GitOps workflow. Check **metadata/status only**:

```sh
kubectl -n argocd get application openbao-config
kubectl -n openbao get secret k8up-repo-password r2-credentials -o name
kubectl -n openbao get pods
```

Never fetch or decode the Secret data to verify this. Argo sync waves inside
the backups chart apply both Secrets before the Schedule on a fresh sync; an
already-created Schedule remains present until reconciliation, so do not count
wave ordering as a repair for an existing failed Job. This K8up Schedule alone
is **not** an independent restore path for the OpenBao data that holds its own
backup credentials.

## Local validation/tests

```sh
ruby tests/verify-bootstrap.rb
ruby tests/verify-application-health.rb # requires Lua
ruby tests/verify-longhorn-encryption.rb
ruby tests/verify-openbao-access.rb
ruby tests/verify-openbao-pki.rb # staged activation gate + offline Kustomize checks
ruby tests/verify-postgresql.rb # HA/storage/backup/policy manifests + pinned chart renders
ruby tests/verify-redis.rb # dormant operator, storage and observability preparation
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_openbao_*.py'
ruby tests/verify-ingress.rb
ruby tests/verify-charts.rb # Helm + network access to pinned chart repositories
```

The ordinary checks validate the **publishable infra repo** without private Git.
`verify-charts.rb` downloads and renders the seven pinned Helm chart sources plus
the safe local templates without contacting Kubernetes. With a sibling private
checkout, `ruby tests/verify-private-values.rb` checks the private values and host
Gateway wiring. Passing checks is not a
storage, certificate, recovery, or live readiness test.
See [tests/README.md](tests/README.md) and [02-controllers/longhorn/README.md](02-controllers/longhorn/README.md)
for limits and storage preflight gates.

The Helm values keep local JSON schemas, linked by
`# yaml-language-server: $schema=...` comments in their `values.yml`. Argo CD
fetches the pinned source charts and renders them with these values. VS Code's
Red Hat YAML extension uses these for completion and diagnostics in single- or
multi-root workspaces. The Kubernetes extension does not consume Helm values
schemas. Checked-in override values and schema snapshots are never rewritten on
opening the repo. Of the pinned charts, OpenBao ships an authoritative schema;
the other chart schemas are local snapshots. Run the VS Code **Validate pinned Helm values** task for
an explicit render after changes.

## Boundaries

OpenBao runs three Raft server pods. New Longhorn-backed volumes use three
storage replicas and `Retain`; existing volumes must be expanded separately to
three replicas. Verify replica placement, Raft quorum and recovery. Host storage
prerequisites, a disposable PVC write/read/reattach test, OpenBao
unseal/recovery and an independent backup restore must pass before relying on
it. Monitoring uses right-sized encrypted three-replica Longhorn claims (Grafana
2Gi, Prometheus 20Gi with 15d/18GB retention, Alertmanager 1Gi). OpenBao explicitly
uses 1Gi each for every Raft data and audit claim. The Pi's 128 GB device is the
limiting node, so capacity decisions use its effective Longhorn-allocatable
space rather than nominal disk size. Replication is not HA or a backup. Before changing a running wave-1 monitoring deployment, see
[the monitoring migration notes](03-core/kube-prometheus-stack/README.md).
