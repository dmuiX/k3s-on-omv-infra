# OIDC proxy

Small Git-sourced Helm chart for a dedicated oauth2-proxy per service. Supply
`issuerURL` and `redirectURL` from Live using Argo CD's `$live` values reference.
`name`, `namespace`, `upstream`, `scope` and `allowedGroup` default to the
existing Longhorn contract. A consumer can opt into a pod security context and
Istio Ambient enrollment without changing Longhorn. Set `passBasicAuth: false`
for upstreams that use their own API identity and must not receive an injected
`Authorization` header.

For OpenBao providers that must avoid an ingress hairpin, `openbaoBackchannelURL`
sets the same provider path on an internal mesh endpoint. The browser uses the
public HTTPS authorization and callback URLs; oauth2-proxy fetches the token and
JWKS over the mesh and still validates the public issuer. OpenBao must include
the username and group claims in the ID token. Standard OIDC discovery remains
the default when this value is unset.

Only the Deployment, Service and inputs ConfigMap are owned here. The existing
ServiceAccount and VSO credential resources remain in
`06-platform/longhorn-config/resources/oidc-proxy.yaml`. No metrics resources or credentials are generated. Each consumer must supply
its own ServiceAccount and Secret (`client-id`, `client-secret`, `cookie-secret`)
matching the proxy name. See the [Tekton Dashboard](../../docs/tekton-dashboard.md)
for a second consumer.

Offline regression test: `python -m unittest discover -s tests -p test_oidc_proxy.py`.
The fixture captures the complete original Deployment and Service. The complete
original ConfigMap is compared by canonical SHA-256 to keep Live URLs private.
