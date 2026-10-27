# openbao-oidc

Narrow Git-sourced Helm component for Radar, Audiobookshelf and Longhorn's
existing OpenBao identities. It emits only `Group`, `OidcAssignment`, `OidcKey`,
`OidcScope`, `OidcClient` and `OidcProvider` from the existing Crossplane API.
No arbitrary manifests, workload auth, VSO delivery, credentials, proxy,
configurator, human membership or platform profile scope are generated.

## Interface

All inputs are under `openbaoOIDC`. Complete, non-sensitive examples are in
`tests/fixtures/*-values.yaml`. There is deliberately no working default client,
`allow_all` assignment, administrator group or administrator claim. Default
rendering fails validation; every integration must explicitly supply its policy.

| Field | Meaning |
| --- | --- |
| `providerConfigName`, `deletionPolicy` | Explicit existing provider and lifecycle policy (`Orphan` for all three consumers). |
| `key` | `name`, `algorithm` (RS256), explicit `allowedClientIds`, `rotationPeriod`, `verificationTtl`. |
| `scopes[]` | Existing `name` and exact OpenBao claim `template` string; never evaluated by Helm `tpl`. |
| `client` | Existing `name`, `key`, nonempty `assignments`, HTTPS `redirectUris`, `idTokenTtl`, `accessTokenTtl`. Always confidential. |
| `client.connectionSecret` | Optional existing `name` and `namespace`. Omitted for Longhorn; do not invent a new Secret there. |
| `provider` | Existing `name`, complete HTTPS `issuerURL`, explicit `clientRefs` and `scopeRefs`. Host/port are extracted without changing the issuer. |
| `assignments[]` | Optional existing `name` and nonempty `groupRefs`. No implicit group assignment. |
| `groups[]` | Optional existing `name`, `externalName`, `type: internal`, `externalMemberEntityIds`. No membership management. |
| `syncWave` | Optional string on each key, scope, client, provider, assignment or group; omitted means no annotation, not wave `0` authored. |

Resource names are explicit, not derived from the release. Crossplane identities
are cluster-scoped and retain their original metadata (no new labels or
namespace). Only the client's connection Secret reference contains a namespace.
Unknown integration/object fields and missing required fields fail schema
validation. Other root keys are allowed so the same Live values file can serve
both a workload chart and this chart. No secret payloads belong in these values.

The existing wildcard **key** allowlist avoids the generated-client/key cycle;
the dedicated provider's explicit client references constrain token issuance.
This does not make a broad client assignment a default. Radar alone explicitly
retains `allow_all` for authentication; its separate admin-group authorization
is unchanged. Audiobookshelf and Longhorn retain their distinct group-based
assignments. Audiobookshelf references the existing platform-owned
`oidc-profile`; it does not create it.

## Adoption

Each existing Application has one additional Git path source,
`charts/openbao-oidc`, using `$live` values. No dependency archive, chart registry
or publishing pipeline is needed: the existing Argo Git-chart mechanism handles
this local chart directly. Destination namespaces and Application identities
stay unchanged, including Audiobookshelf's disabled automated sync.

- Radar's public supporting-resource Kustomization no longer includes OIDC;
  Live still overlays that base, without the old OIDC patches.
- Audiobookshelf's workload chart retains VSO/access resources and API
  configurator. Its identities move to this source in the **same** Application;
  the existing connection Secret namespace and all waves are explicit.
- Longhorn's private source uses a Kustomization selecting only its existing
  route/trust documents, excluding the new values file. Its proxy, cookie
  Secret delivery and access resources remain separate and unchanged.

Before any sync, publish the Infra chart, then the dependent Apps/Live revisions
as a coordinated batch. Do not refresh/sync a partial cross-repository state.
Read-only deployment preflight must verify the existing Application tracking,
external names, client identity and connection Secret references without
retrieving secret contents. Repository renders cannot prove live ownership.
Do not delete/recreate a managed resource or regenerate credentials to adopt it.
Automatic pruning remains disabled. No cluster action is part of this change.
Rollback restores the old concrete sources/templates and Live inputs while
removing the shared source in the same desired state; never leave two generators
for a managed resource. Any live operation requires separate approval.

## Offline tests

```sh
ruby charts/openbao-oidc/tests/test_render.rb
helm lint charts/openbao-oidc --strict \
  -f charts/openbao-oidc/tests/fixtures/radar-values.yaml
```

Fixtures were captured from the pre-edit working tree, including the user's
existing Radar Kustomize changes: Radar was built using a local copy of its base,
Audiobookshelf rendered with its existing Live values, and Longhorn used its
existing authored objects. Fixtures replace only private issuer/callback
authorities with reserved example domains. All other fields, claim strings,
references, ordering annotations and absent annotations are compared exactly.
No Secret data is captured. In-memory/local temporary pre/post comparisons also
verified the real Live URLs and all 27 Audiobookshelf objects unchanged.

The suite tests full-object equivalence, required fields, unsafe URLs, empty
assignments, key/provider consistency and unknown fields. With sibling Apps/Live
checkouts it additionally renders their actual values, verifies native source
wiring, rejects duplicate identities, builds offline overlays and proves the
six existing Audiobookshelf VSO/access objects retain their exact specs.
Legacy repository tests that assume OIDC remains in the old concrete sources
must be updated by their owners; those files are outside this component's scope.
