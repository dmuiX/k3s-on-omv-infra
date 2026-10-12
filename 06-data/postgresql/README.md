# Central PostgreSQL platform

`Application/postgresql` is the Wave-6 owner for CloudNativePG, the Barman Cloud
plugin and `platform-postgres`. The root application now discovers it normally.

## Grafana database credentials POC

OpenBao is the source of truth and generates/rotates the fixed `grafana` database
login via its database secrets engine static role (`database/static-creds/grafana`).
The role must exist before rotation is enabled. OpenBao's PostgreSQL configuration
uses the stable `platform-postgres-rw.postgresql.svc` endpoint and a dedicated
manager credential stored in OpenBao KV v2 at `kv/postgresql/openbao-manager`,
with keys `username` and `password`. VSO materializes that pair as
`postgresql/openbao-manager-credentials`; this is a prerequisite, not a literal
credential in Git.

CNPG `DatabaseRole/grafana` deliberately has no `passwordSecret`: CNPG creates
the fixed login role but does not manage its password. It retains the exact
`databaseRoleReclaimPolicy: retain` safeguard. OpenBao static-role rotation
updates that login, and VSO 1.6.0 reads `database/static-creds/grafana` with
`allowStaticCreds: true`, publishing `grafana-db-credentials` in the Grafana
namespace and requesting a Grafana Deployment restart on updates.

The provider-vault package is pinned to v4.0.4. Its ProviderConfig requires
Kubernetes auth to OpenBao, appropriate provider service-account RBAC, and
read access to the manager Secret. These auth role, policy, engine configuration,
connection, and RBAC prerequisites require OpenBao-side bootstrap. The CRD/API
shape and chart pins are a proof-of-concept and must be validated against the
installed provider before activation. Do not apply to a live cluster from this
branch; no live systems were contacted.

VSO and Crossplane applications are installed at sync wave 2, before the
PostgreSQL Wave-6 application. Resources are ordered so the CNPG role/cluster
must exist before OpenBao can successfully establish/rotate the static role;
verify initial role creation and first rotation before enabling the Grafana
credential consumer. Never place generated credentials in Git.

## Platform safeguards

CloudNativePG and Barman chart versions remain pinned in `app.yml`. CNPG role
and database reclaim policies remain `retain`; PostgreSQL storage, backup,
network policy, and application connection protections are unchanged. OpenBao
manager bootstrap is supplied by sibling Ansible work; this repository does not
implement that bootstrap.
