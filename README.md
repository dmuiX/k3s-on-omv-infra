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
├── crossplane/             OpenBao provider and provider configuration
└── openbao/                secret store
04-secrets/
├── vault-secrets-operator/ OpenBao-to-Kubernetes secret synchronization
└── vault-secrets-webhook/  admission-time secret injection
05-platform/
├── public-certificates/    wildcard certificate/issuers after secrets
├── crossplane/             OpenBao ProviderConfig after provider installation
├── openbao-pki/            cert-manager identities, issuers and internal certificate
├── argocd/
├── longhorn/
└── openbao/                early UI routes, usable when wildcard TLS becomes ready
06-data/
├── openbao-backups/        OpenBao backup schedule
├── cloudnative-pg/         CNPG controllers and PostgreSQL data in `cloudnative-pg`
└── redis/                  dormant Redis operator preparation (`application.yml`)
08-applications/
└── radar/                  Radar-owned PostgreSQL credential synchronization
08-monitoring/
├── grafana-route/          Grafana route after database credential delivery
└── kube-prometheus-stack/  monitoring and its PostgreSQL credential synchronization
```

The first directory level is the parent sync wave; the second keeps component
ownership separate. Applications in one wave are independent and may start in
any order. A real dependency belongs in a later wave, not in an alphabetic
component name. The wave-1 CRD-only Application renders monitoring CRDs before
controllers emit ServiceMonitors. Longhorn and OpenBao precede Crossplane and the
Vault Secrets Operator; the Crossplane package installs alongside OpenBao,
while its ProviderConfig follows in wave 5. The dedicated `cloudnative-pg`
controller namespace and PostgreSQL data resources start automatically in wave 6
after OpenBao PKI,
manager credential, and provider prerequisites. Each consuming Application owns
its namespace-local VSO resources in wave 8. Grafana's credential resources use
negative resource waves so its PostgreSQL credential is healthy before the
monitoring chart is applied.
Longhorn also precedes OpenBao PVCs; the secrets webhook follows OpenBao.
Wildcard certificate issuance and UI routes share wave 5 after OpenBao-backed
Cloudflare custody and webhook activation; routes may reconcile first but become
usable only when the Gateway certificate is ready. Certificate configuration
retries the expected create-only OpenBao prerequisite indefinitely with
exponential backoff capped at five minutes, avoiding a tight reconciliation
loop while an operator completes the hidden bootstrap prompt. The backup schedule remains
separately gated until its OpenBao credentials exist. Argo CD's health customization
shares wave 1 with other Applications: **seed and verify it separately before a
first root sync**. Changing the existing Argo CD backend to HTTP also requires a
controlled `argocd-server` restart; plan that before enabling its external route.

`infra.yml` is the **one** Argo CD root for the complete steady-state Application
inventory, including PostgreSQL and
[`05-platform/openbao-pki/`](05-platform/openbao-pki/workload/README.md); there is
no mutable Ansible rollout state or PostgreSQL root exclusion. Ansible completes
OpenBao initialization, seal/custody handling, PKI ceremony, trust bootstrap,
and protected manager/R2 input before dependent resources reconcile. The PKI Application owns only cert-manager
identities/RBAC, ClusterIssuers, and the internal Certificate. Upstream
controllers are Argo multi-source Applications: one version-pinned Helm/OCI chart
plus values from this Git repository. Route, certificate, and backup resources render the local
`charts/cluster-config` chart with private values from the live repository. The
cert-manager Application separately owns
`02-controllers/cert-manager/network-policy`, which renders its private API
endpoint `/32` list from the same Live values in wave 2. K3s evaluates that
connection after Service DNAT on endpoint port 6443; no LAN address is committed
to this public repository.
Authored Kustomize resources remain in Git, but rendered upstream chart output
is deliberately not vendored. Argo directly owns the encrypted Longhorn
StorageClass while chart generation is disabled, so no duplicate template owner
is required. Certificate issuance and UI routes follow OpenBao/webhook together
in wave 5. PostgreSQL and backup configuration reconcile in wave 6. Consumer-owned
credentials and Grafana/monitoring reconcile in wave 8 with resource-level ordering.

The local chart installs no controller and defaults to `component: none`.
A rendered route does not prove working HTTPS: check Certificate Ready and the
K3s Gateway listener.

Argo CD needs read-only private Git access **before** the first root sync.
This cannot depend on OpenBao, which is installed by that sync: seed the Git
credential and child-Application health customization in a separately approved
bootstrap. Secrets such as Cloudflare tokens and backup passwords live
in OpenBao, not Git. The public templates use non-secret `vault:` references;
the private values file contains identifiers and private network endpoints, not
credentials.
The root and child Applications' automation and pruning settings require review before deployment.
The Grafana subchart creates its initial random administrator Secret in-cluster.
Argo ignores only the generated `admin-user` and `admin-password` data fields so
subsequent Helm renders do not rotate them. The values are never committed or
logged; inspect or rotate them only through an explicitly approved secret-access
procedure.

The `raspi4` node carries `CriticalAddonsOnly=true:NoSchedule`. Infra
controllers and PostgreSQL explicitly tolerate that taint.
`kube-prometheus-stack` is the deliberate exception; even its node exporter
excludes `raspi4` to reserve the Pi's limited capacity.

### OpenBao configuration ownership

OpenBao API configuration is limited to explicit ownership boundaries. The
bootstrap Ansible workflow owns mounts, Kubernetes auth setup and trust policies,
PKI configuration, initialization/seal/custody operations, and protected
one-time inputs. The pinned Crossplane provider is allowed only to manage the
PostgreSQL connection configuration and static database roles; it cannot manage
OpenBao policies, auth roles, mounts or KV data. Argo CD owns the Kubernetes
resources, while Crossplane reconciles only that bounded OpenBao database state.
Do not add generic OpenBao Jobs, CronJobs or polling reconcilers.

The root intentionally retains `automated.prune: false`, and the former
`openbao-access-config` Application had no resources finalizer. Therefore this
Git removal cannot clean an already deployed copy automatically. During the
separately approved rollout, remove that legacy child Application and its
Job/CronJob, ConfigMap, NetworkPolicy, ServiceAccount, and RBAC through a
reviewed cascading Argo decommission, then verify that no such workload remains.
Do not treat a clean Git diff as proof of live removal.

Before enabling any K8up Schedule, run the Bootstrap repository's guarded
`gitops_bootstrap_action=k8up-credentials`. It create-once maintains
`kv/k8up/repository-password#password` and the dedicated
`kv/k8up/r2-credentials` fields `access-key-id` and `secret-access-key`.
Exact legacy entries may be reused only after an explicit interactive choice;
new R2 values are read through hidden prompts and never enter Git, command
arguments, files, Kubernetes staging Secrets, or logs.

Ansible attaches the reviewed webhook and backup policies to their roles.
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
ruby tests/verify-openbao-pki.rb # native Kubernetes resources + offline Kustomize render
ruby tests/verify-postgresql.rb # HA/storage/backup/policy manifests + pinned chart renders
ruby tests/verify-redis.rb # dormant operator, storage and observability preparation
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
