# GitOps service credentials

## Ownership and native mechanisms

- provider-vault 4.0.5 creates the Longhorn OIDC key, scope, admin-group assignment, confidential client and provider. The provider permits only its generated client ID; the assignment permits only `radar:admin`. Radar requests its actual custom `radar` scope, including username/groups. Dedicated signing keys allow `*` because provider-vault's key schema has no client-ID reference field and the client itself requires the key to exist first. Provider client allowlists remain explicit, not `*`.
- CloudNativePG manages the non-superuser `openbao_manager` login/password with `spec.managed.roles` and a VSO-delivered basic-auth Secret. `CREATEROLE` lets OpenBao create the two nonprivileged application roles and subsequently rotate roles it administers; the manager cannot create databases, replicate or bypass row security.
- VSO delivers existing OpenBao values to consumers. It does not generate random values or issue Argo CD API tokens. Radar chart 1.17.0 and the plain oauth2-proxy Deployment accept existing Secrets; Radar's empty-secret fallback is process-local, not durable shared secret custody.
- Argo CD declares an enabled, **apiKey-only** `radar` account. Its policy permits application/project reads only. The Job uses Argo CD 3.5.5's supported `argocd --core account generate-token` command, rather than copying the admin password or implementing JWT signing. It then verifies that the real Argo server accepts the token over certificate-verified TLS.

Sources inspected: [Radar values](https://github.com/skyhook-io/radar/blob/v1.17.0/deploy/helm/radar/values.yaml), [Argo account API](https://github.com/argoproj/argo-cd/blob/v3.5.5/server/account/account.go), [Argo core mode](https://github.com/argoproj/argo-cd/blob/v3.5.5/cmd/argocd/commands/headless/headless.go), [CNPG managed roles](https://cloudnative-pg.io/docs/1.28/declarative_role_management/), the installed provider-vault CRD schemas, and [OpenBao OIDC implementation](https://github.com/openbao/openbao/blob/v2.7.1/internal/vault/identity_store_oidc_provider.go).

## Bounded gap-filling Job

`06-platform/openbao-config/resources/service-credentials.yaml` contains one Sync-hook Job, before its Secret consumers. It creates only:

| OpenBao KV path | Contents |
| --- | --- |
| `longhorn/oidc-proxy` | Native client ID/secret plus a 32-byte random cookie key |
| `postgresql/openbao-manager` | Fixed username and a random password |
| `radar/auth` | Durable random session-signing key |
| `radar/argocd` | Natively issued read-only account token and stable token ID |

Each entry carries an ownership marker. Reads, CAS-0 writes and read-back verification preserve existing values. The Job's OpenBao ACL has `create`, **not `update`**, on exactly these four data paths. A missing current value with retained metadata is a tombstone, not permission to regenerate. Foreign/malformed values stop synchronization. No root token or human password enters the Job. Secrets and CLI output are withheld from logs; scratch data and copied native CLI live on memory-backed volumes.

The core-mode account API requires Argo namespace informers. Consequently the Job can read/watch Argo Secrets/configuration, but can update only `argocd-secret`, where Argo stores token registrations. This is a privileged credential-issuer identity, not the Radar application identity. It has no cluster-wide RBAC. Projected Kubernetes and OpenBao audience tokens are separate and short-lived.

A retry reuses KV values. If issuance succeeded but custody was interrupted, it revokes only its own fixed orphan token ID before reissuing. Foreign account tokens are not silently deleted. A missing/revoked token behind an existing KV entry fails rather than silently rotating a consumer's credential. Normal Argo synchronization reruns the hook; its deadline and retry count are bounded. Non-expiring application-token behavior matches the chart's native integration; rotation is deliberately not implicit.

## PostgreSQL and mesh details

The PostgreSQL plugin parses statement strings on semicolons unless the string encodes a JSON array. The application rotation statements therefore JSON-wrap their `DO` block, preserving it as one SQL command. Existing privileged roles are rejected. `ALTER ROLE` does not request superuser-only `NOSUPERUSER`/`NOREPLICATION` attribute changes; newly created roles still explicitly prohibit those privileges.

NetworkPolicies permit only the relevant database/client HBONE paths in addition to application ports. The CNPG webhook has a workload/port-specific PeerAuthentication exception on 9443: the Kubernetes API server cannot originate HBONE, but still authenticates CNPG's HTTPS certificate. Database traffic remains STRICT. Radar ingress uses the actual `istio-gateway` namespace.

## Validation

`OPENBAO_TEST_BINARY=/path/to/verified/bao tests/run-offline-tests.sh` exercises the existing bootstrap suite and the new credential tests. New real-OpenBao tests verify create-only ACLs, immutable first/resumed KV writes, foreign/tombstoned-value refusal, real authorization-code/PKCE token exchange, custom username/group claims, provider client rejection and group-assignment rejection. Native Argo/Radar charts render successfully. Run full tests from a clean repository workspace: the unrelated untracked `argo-cd/` chart checkout is not repository input and must not be included by YAML-glob tests.

Offline tests do not prove live deployment acceptance. Human MFA, R2, SMB and SMTP inputs remain attended; the playbook's final health and Apps gates are unchanged.
