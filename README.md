# K3s on OMV — reusable infrastructure base

This repository contains reusable Argo CD Applications for the initial K3s
deployment on OMV. The first phase uses one physical node; expansion to three
physical nodes is planned after recovery and hardware prerequisites are met.
It contains **complete resource templates** for HTTPRoutes,
DNS-01 certificates, OpenBao backups and manual restores in
[`charts/cluster-config/`](charts/cluster-config/), but no *real* hostnames,
ACME email, backup account identifiers or backup destination. The chart defaults
use `example.invalid`; its default `component: none` deploys nothing. A single
private values file supplies the real non-secret identifiers. The GitHub infra
repository is public: review files and history before each push.

## Ownership and rollout

K3s owns Traefik, its Gateway and GatewayClass; the host `HelmChartConfig`
lives in a separate bootstrap repository. This repo does not install Traefik or
External-DNS. It uses Argo CD (already installed) for these pinned charts:

```text
01-argocd-bootstrap/       child-Application health customization
01-argocd-server-config/   existing Argo CD server backend configuration
01-kube-prometheus-stack/  ephemeral monitoring and its CRDs
02-cert-manager/           certificate controller
02-k8up/                   backup controller
02-longhorn/               single-node storage
03-openbao/                secret store
04-vault-secrets-webhook/   admission-time secret injection
05-certificates/           wildcard certificate/issuers after secrets
06-*-route/                UI routes after certificate configuration
06-openbao-backups/        OpenBao backup schedule
```

Prefixes are the first parent sync wave. Monitoring's CRDs precede the
ServiceMonitors emitted by later charts; Longhorn precedes OpenBao PVCs; the
secrets webhook follows OpenBao. The real certificate and backup schedule wait
until OpenBao and the webhook are initialized. Argo CD's health customization
shares wave 1 with other Applications: **seed and verify it separately before a
first root sync**. Changing the existing Argo CD backend to HTTP also requires a
controlled `argocd-server` restart; plan that before enabling its external route.

`infra.yml` is the **one** Argo CD root. All Applications and complete resource
templates are in this repo. The route, certificate and backup Applications
render `charts/cluster-config/` with the **same private values file**; the
private Git source has `ref: values` and no manifest path. Backends run in
waves 1–3; certificate issuance follows OpenBao/webhook (wave 5), and all UI
routes and backups are configured in wave 6. Routes cannot block the controllers
needed to issue their TLS certificate. The private repo contains only
non-secret values, not copies of Applications or resource manifests. The local
`charts/cluster-config/` chart exists because Kubernetes needs concrete route
hostnames, certificate DNS names, ACME email and backup destinations. Argo CD
can pass the private `$values/...` file to Helm, but cannot substitute those
values into plain YAML on its own. Helm renders the public templates; the chart
installs no controller, and its default `component: none` renders nothing. An
a rendered route does not prove working HTTPS: check Certificate Ready and the
K3s Gateway listener.

Argo CD needs read-only private Git access **before** the first root sync.
This cannot depend on OpenBao, which is installed by that sync: seed the Git
credential and child-Application health customization in a separately approved
bootstrap. Secrets such as Cloudflare tokens and backup passwords live
in OpenBao, not Git. The public templates use non-secret `vault:` references;
the private values file contains identifiers, not credentials.
The root and child Applications' automation and pruning settings require review before deployment.

## Local validation/tests

```sh
ruby tests/verify-bootstrap.rb
ruby tests/verify-ingress.rb
ruby tests/verify-charts.rb # requires Helm and network access to pinned charts
```

The first two check the **publishable infra repo** without private Git. The
third renders all six pinned charts and the safe public resource templates.
With a sibling private values checkout, `ruby tests/verify-private-values.rb`
checks the actual rendering and host Gateway wiring. Passing renders is not a
storage, certificate, recovery, or live readiness test.
See [tests/README.md](tests/README.md) and [02-longhorn/README.md](02-longhorn/README.md)
for limits and storage preflight gates.

Only the five configured Helm charts keep local JSON values schemas, linked by
`# yaml-language-server: $schema=...` comments in their `values.yml`. Copied
CRDs are not checked in; the pinned charts supply them. VS Code's
Red Hat YAML extension uses these for completion and diagnostics in single- or
multi-root workspaces. The Kubernetes extension does not consume Helm values
schemas. Checked-in override values and schema snapshots are never rewritten on
opening the repo. Of the pinned charts, OpenBao ships an authoritative schema;
the other four do not. Run the VS Code **Validate pinned Helm values** task for
an explicit render after changes.

## Boundaries

In the first phase, OpenBao runs one Raft server pod. Its Longhorn-backed
volumes use one storage replica and `Retain` on the OMV node. Adding nodes
does not automatically add OpenBao pods or data copies. After three suitable
nodes and disks are ready, separately scale OpenBao to three pods, configure
three Longhorn replicas for new volumes, and increase the replica count on
existing volumes; verify placement, Raft quorum and recovery. Host storage
prerequisites, a disposable PVC write/read/reattach test, OpenBao
unseal/recovery and an independent backup restore must pass before relying on
it. Monitoring data is ephemeral during bootstrap.
