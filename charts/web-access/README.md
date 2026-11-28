# web-access

Unified feature-flagged Git-sourced Helm chart replacing `charts/service-route`,
`charts/openbao-oidc`, `charts/oidc-proxy`, and optional `credential-custody`
ConfigMap delivery for web applications.

## Feature Blocks

| Block | Default | Resources Rendered |
| --- | --- | --- |
| `route` | `enabled: true` | Gateway API `HTTPRoute` with explicit `backendRefs` defaults (`group: ""`, `kind: Service`, `weight`) |
| `oidc` | `enabled: false` | Crossplane OpenBao `Group`, `OidcAssignment`, `OidcKey`, `OidcScope`, `OidcClient`, `OidcProvider` + `credential-custody` ConfigMap (disable via `oidc.credentialHelper: false`) |
| `proxy` | `enabled: false` | Dedicated `oauth2-proxy` `Deployment`, `Service`, `<proxy>-inputs` `ConfigMap` + optional Sync-Hook `provisioner` (`ServiceAccount`, `ConfigMap`, `Job`) |

## Design Contracts

- **Explicit identity**: `name` and `namespace` are required and never derived from `.Release.Name`.
- **Single Live values file (`web-access-values.yml`)**: Supply `route.hostnames`, `oidc.issuerURL`, and `oidc.redirectURLs` from the private Live repo. When `proxy.enabled` is `true`, `proxy.issuerURL` and `proxy.redirectURL` automatically reuse `oidc.issuerURL` and `oidc.redirectURLs[0]`.
- **Strict schema & fail-closed validation**: Unknown fields, non-HTTPS OIDC URLs, HTTP backchannel without Istio Ambient, and empty assignments fail rendering.
- **Byte-identical `credential-custody`**: `tests/test_web_access.py` enforces that `templates/credential-custody.yaml` matches `adapters/credential-custody/resources.yaml` (which remains for non-web-access consumers).

## Offline Tests

```sh
helm lint charts/web-access --strict -f tests/fixtures/web-access/full-stack-values.yaml
python3 -m unittest discover -s tests -p test_web_access.py -v
```
