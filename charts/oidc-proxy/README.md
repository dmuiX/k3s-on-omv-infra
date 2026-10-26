# OIDC proxy

Small Git-sourced Helm chart for the existing Longhorn oauth2-proxy runtime.
Uses native Helm templates and required values, with no manifest passthrough or
Kustomize forwarding. Supply `issuerURL` and `redirectURL` from Live using Argo
CD's `$live` values reference. `name` and `namespace` default to the existing
Longhorn identities; the image digest, upstream, arguments, probes, resources,
security context and credential references retain the original contract.

Only the Deployment, Service and inputs ConfigMap are owned here. The existing
ServiceAccount and VSO credential resources remain in
`06-platform/longhorn-config/resources/oidc-proxy.yaml`. No metrics resources or
credentials are generated. Renaming the proxy also requires corresponding
external credentials and ServiceAccount names.

Offline regression test: `python -m unittest discover -s tests -p test_oidc_proxy.py`.
The fixture captures the complete original Deployment and Service. The complete
original ConfigMap is compared by canonical SHA-256 to keep Live URLs private.
