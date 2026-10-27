# Shared required OpenBao values

The wave-2 `vault-secrets-operator` Application owns the source-only
`SecretTransformation/vault-secrets-operator/required-openbao-value` through a
second raw-resource source alongside the pinned VSO chart. Consumers explicitly
reference its namespace. Authentication, policies, identities and ServiceAccounts
remain unchanged. The coordinated [shared connection](vso-connection.md) replaces
consumer-local transport configuration without sharing authentication.

## Contract

`requiredValue` receives a dictionary with `key` and `value`. Native Go templates
and Sprig `empty`/`fail` reject missing/null values, empty strings/containers,
false and zero. Successful values are emitted unchanged, including whitespace.
This is not credential-strength, whitespace, type or URI validation. Errors name
the missing key, never its value. VSO does not expose Helm's `required` function.

Consumer transformations retain `excludeRaw: true`. Hyphenated input keys use
`get`; nested Helm templates preserve the VSO Go-template expressions. Cookie
Base64 conversion, etcd HTTPS-prefix removal, database URL escaping and
Alertmanager `%q` quoting stay consumer-owned. Short-circuit `and` passes empty
raw values through to validation before URL escaping/quoting, so encoding cannot
turn missing input into an apparently valid value. Constants and defaults remain
constants, including etcd region `auto`. `excludeRaw` suppresses `_raw`, not
arbitrary KV fields; this change does not introduce a general source-field
allowlist. Grafana admin custody, its field filters and its template regression
tests belong to the separate Grafana change (#66), not this transport/validation
change. Grafana's existing database-only identity and ServiceAccount are preserved.

## Consumers

- DNS: Cloudflare API token.
- Backup credentials: SMB username/password and etcd R2 keys/endpoint.
- Longhorn: OIDC client ID/secret and cookie key.
- OpenBao snapshots: R2 keys, region and endpoint.
- PostgreSQL: manager credentials and R2 fields/aliases.
- Backup validation: repository password and R2 fields/aliases.
- Monitoring: Alertmanager SMTP fields and repository password.
- `charts/postgres-consumer`: database username/password in split or DSN format.
- Apps: Radar OIDC/auth/Argo CD credentials and enabled Audiobookshelf OIDC.

OpenBao metrics resources, tokens and listener architecture are outside this
change; no metrics transformation or connection is introduced or migrated.
Publish the Infra helper and shared connection before Apps consumer changes.
The coordinated Apps change (#7) consumes this helper; it is not required to
merge this Infra change. Existing unvalidated DNS/backup-validation fields gain
required-value checks; existing validation and successful output bytes remain
unchanged.

## Native API and trust

VSO 1.6.0 supports explicitly namespaced
`destination.transformation.transformationRefs`. Its
[`gatherTemplates`](https://github.com/hashicorp/vault-secrets-operator/blob/v1.6.0/helpers/template.go)
loads source definitions before executing output templates and requires a valid
transformation. The operator's Kubernetes client resolves references; consumer
ServiceAccounts need no added RBAC. The pinned chart grants the operator
cluster-scoped `get/list/watch` access to SecretTransformations. Cross-namespace
reuse is not tenant isolation or permission to edit the helper. Restrict writes
to its namespace/resource: helper changes affect every referencing consumer.

Sync the operator Application/helper first, then consumers. Application waves
express intended ordering, not proof that controller validation has completed.
Absent/not-yet-valid definitions or render errors block destination sync and
retry rather than writing partial output. Existing Secrets are not cleared on
failure, so workloads may keep using previous valid credentials. No live rollout,
secret read or retirement of existing resources is authorized by this change.
Verify installed CRDs/RBAC, helper validity, Argo ownership/health and eventual
consumer reconciliation in a separately approved rollout.

## Offline validation

Use Ansible's Python (existing PyYAML), Helm and Go with sibling Apps/Live
snapshot trees. Set `PLATFORM_APPS_REPO` to the independent Apps #7 candidate
for the helper/transport integration tests. Existing chart-adoption tests use
the separate Apps #6 sibling snapshot; neither topic must inherit the other. `tests/test_backup_validation_templates.py` inventories consumers,
checks explicit references and operator RBAC, and exercises the pinned upstream
Go engine, including empty inputs and successful byte/escape preservation.

```sh
PLATFORM_APPS_REPO=/path/to/independent-apps-vso \
VSO_TEMPLATE_SOURCE=/path/to/vso-v1.6.0 \
VSO_CHART_SOURCE=/path/to/vso-chart \
  python -m unittest discover -s tests -p test_backup_validation_templates.py -v
```

The optional engine/chart checks skip explicitly without cached sources. They
use cached dependencies only; no dependency installation or cluster access is
required. Native CRD checks cover namespaced transformation references and
`excludeRaw`; template checks execute the upstream Go engine, not the operator's
Secret builder or runtime reconciliation. Keep Grafana-specific filter/merge,
extra-source-key and admin-template tests with #66 when that consumer is added.
`tests/test_service_credentials.py` also renders the resolved shared OIDC chart
sources using existing `charts/openbao-oidc/tests/fixtures/*-values.yaml`; this
offline check never starts OpenBao. Actual code exchange and group-assignment
tests remain opt-in via `OPENBAO_TEST_BINARY` and require separate approval to run.
Bootstrap validation, provisioning jobs and non-VSO secret writers
retain their own checks; this helper is not general admission enforcement.
