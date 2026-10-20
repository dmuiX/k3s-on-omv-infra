# Certificate-first bootstrap

The former dependency cycle was Gateway -> Certificate -> Cloudflare token ->
OpenBao -> later waves -> Gateway health. The DNS token is now seeded by the
companion Bootstrap PR before the Infra root syncs.

| Root wave | Application | Required before this wave |
| --- | --- | --- |
| -50 | cert-manager | accepted K3s + Argo CD; DNS Secret already seeded |
| -40 | tls-certificates | cert-manager; ClusterIssuers and explicit Certificates |
| -30 | istio-core | Certificates ready, not just present |
| -20 | istio-ambient (Gateway resources) | Istio controller + wildcard TLS Secret |
| 1 | argocd-config, prometheus-operator-crds | Gateway exists for the Argo CD route |
| 2-8 | existing platform waves | existing provider/storage/OpenBao dependencies |

`tls-certificates` owns the gateway namespace and the Live
`infra/letsencrypt-dns01` resources. That directory must contain only
ClusterIssuers and Certificates, **not** an Istio BackendTLSPolicy.
The companion Live PR moves the latter to `infra/argocd-config` and adds an
explicit wildcard Certificate. The Gateway no longer creates its own prerequisite
through gateway-shim. No fake Healthy assessment, temporary TLS Secret or skipped
certificate verification is introduced.

The wave-6 `letsencrypt-dns01` app now owns only the OpenBao/VSO credential link.
Its VaultStaticSecret updates the Secret created by Bootstrap (`create: false`).
Bootstrap imports the early token into OpenBao once; no second Cloudflare prompt.
Deleting that Secret requires restoring it with the normal bootstrap.

Merge the companion Live and Bootstrap changes together with this PR. For an
existing root sync pinned to the previous commit, a one-time termination of that
stale operation followed by syncing the new revision may be necessary. Do not
reset the cluster. Fresh deployments require only the ordinary deploy.py run.

The live Cloudflare token, Internet/DNS access and successful ACME issuance are
still required. CI validates layout, Application structure and dependency order,
not a real ACME issuance or cluster rollout.
