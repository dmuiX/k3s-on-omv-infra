# Certificate-first bootstrap

The former dependency cycle was Gateway -> Certificate -> Cloudflare token ->
OpenBao -> later waves -> Gateway health. The DNS token is now seeded by the
companion Bootstrap PR before the Infra root syncs.

| Root wave | Application | Required before this wave |
| --- | --- | --- |
| -5 | cert-manager | accepted K3s + Argo CD; DNS Secret already seeded |
| -4 | letsencrypt-dns01-issuer | cert-manager; ClusterIssuers and explicit Certificates |
| -3 | istio-core | Certificates ready, not just present |
| -2 | istio-ambient (Gateway resources) | Istio controller + wildcard TLS Secret |
| 1 | argocd-config, prometheus-operator-crds | Gateway exists for the Argo CD route |
| 2-9 | existing platform waves | existing provider/storage/OpenBao dependencies |

`letsencrypt-dns01-issuer` owns the gateway namespace and the Live
`infra/letsencrypt-dns01` resources. That directory must contain only
ClusterIssuers and Certificates, **not** an Istio BackendTLSPolicy.
The companion Live PR moves the latter to `infra/argocd-config` and adds an
explicit wildcard Certificate. The Gateway no longer creates its own prerequisite
through gateway-shim. No fake Healthy assessment, temporary TLS Secret or skipped
certificate verification is introduced.

The wave-6 `letsencrypt-dns01-credentials` app owns only the OpenBao/VSO credential link.
Its VaultStaticSecret updates the Secret created by Bootstrap (`create: false`).
Bootstrap imports the early token into OpenBao once; no second Cloudflare prompt.
Deleting that Secret requires restoring it with the normal bootstrap.

Merge the companion Live and Bootstrap changes together with this PR. For an
existing root sync pinned to the previous commit, a one-time termination of that
stale operation followed by syncing the new revision may be necessary. Do not
reset the cluster. Fresh deployments require only the ordinary deploy.py run.

## Parent Application rollout gate

Root waves remain health-gated, including Longhorn before monitoring. The custom
Application health check blocks requested/running/terminating operations, failed
or errored syncs, comparison/spec/sync errors, and unreconciled source or destination
changes. Actual child health is propagated; unhealthy workloads are never marked
Healthy to bypass a wave.

An `OutOfSync` child may release its wave only after a successful full, non-dry-run
sync of exactly the currently compared revisions and source configurations. A
selective sync or an old successful revision is not sufficient. Drift remains
visible on the child Application. A `Synced` child does not need a redundant sync
for a repository commit that changes no rendered resources, but still requires a
previous successful operation and a comparison of its current spec.

This is not a classifier for harmless drift: any drift at already-deployed inputs
can pass when the child remains Healthy. Newly discovered revisions still wait for
a successful deployment. Automated self-heal remains enabled. Evidence is checked
with the actual Argo CD offline Lua evaluator in `tests/test_application_health.rb`.
The supported customization mechanism is documented in [Argo CD resource health](https://argo-cd.readthedocs.io/en/stable/operator-manual/health/).

The live Cloudflare token, Internet/DNS access and successful ACME issuance are
still required. CI validates layout, Application structure and dependency order,
not a real ACME issuance or cluster rollout.
