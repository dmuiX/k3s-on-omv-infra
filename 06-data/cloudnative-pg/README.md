# CloudNativePG platform

`Application/cloudnative-pg` is the automatically reconciled Wave-6 owner for
CloudNativePG, the Barman Cloud plugin and `k3s-postgres`. Controllers,
webhooks, certificates, database clusters, credentials, ObjectStores, PDBs and
NetworkPolicies all run in the dedicated `cloudnative-pg` namespace.

The chart uses namespace-scoped RBAC and reconciliation. It remains non-pruning
until backup, restore, PITR, failover and application-connectivity acceptance is
completed. Moving an existing installation is a controlled migration, not an
ordinary rolling update. Never enable broad pruning as a namespace-cleanup
shortcut; retained PostgreSQL resources and recovery evidence share the
Application.

## OpenBao-managed database credentials

The central `vault-secrets-operator` Application owns only the operator. This
CloudNativePG Application owns its namespace-local ServiceAccount,
`VaultConnection`, `VaultAuth`, and bootstrap `VaultStaticSecret` resources so
secret delivery cannot own or prune the database namespace.

OpenBao is authoritative for the fixed Grafana and Radar login passwords. Each
login is represented by an OpenBao database static role with a 30-day rotation
period. CNPG creates and retains each login role but has no `passwordSecret`, so
it does not overwrite OpenBao's password. OpenBao's PostgreSQL connection uses
the stable `k3s-postgres-rw.cloudnative-pg.svc` endpoint and a dedicated
`openbao_manager` login. Its randomly generated password is created once in
OpenBao KV v2 at `kv/postgresql/openbao-manager` with CAS=0 by the guarded
Bootstrap Ansible action. CNPG receives that credential through VSO; it is not
stored in inventory or Git.

The manager login is not a superuser and cannot create databases. It has only
`CREATEROLE`, a connection limit of five, and a dedicated Kubernetes Role that
lets the Crossplane provider read only the manager Secret. The provider's
OpenBao policy is limited to the `k3s-postgres` database configuration and
static-role endpoints; it cannot manage ACL policies, auth roles, mounts or KV
data. Ansible installs narrow read-only VSO policies for the manager, Grafana,
and Radar identities as part of the one-time OpenBao trust bootstrap; it never
creates per-application passwords.

Crossplane's OpenBao provider (v4.0.4) reconciles the database connection and
static roles. VSO 1.6.0 reads `database/static-creds/{grafana,radar}` with
`allowStaticCreds: true`, materializes namespace-local Kubernetes Secrets, and
restarts Grafana when its password rotates. The Radar credential is synchronized
for its future consumer; no Radar workload restart target is configured yet.
The applications are ordered in Waves 6–8, but offline checks do not prove
operator readiness or that VSO has created destination Secrets before later
Applications reconcile. Controllers are expected to retry until prerequisites
exist; verify this behavior during the live PoC.

Grafana and the provider use `sslmode=require`, which encrypts PostgreSQL
transport but does **not** verify the server CA or hostname. The PostgreSQL
server Secret is namespace-local; Grafana cannot mount it across namespaces.
Trusted CA verification remains a production prerequisite and requires an
explicit CA distribution mechanism into Grafana's namespace.
Switching an existing Grafana installation from SQLite to PostgreSQL also
requires a separately planned and accepted data migration; this manifest change
does not migrate existing Grafana data.

The provider-vault package is Vault-focused and not officially certified for
OpenBao. Offline contract tests check selected manifest fields but do not
validate them against the provider's installed CRDs. OpenBao compatibility,
first static-role creation, rotation privileges for CNPG-created roles, VSO
restart behavior, and database connectivity have not been validated on a live
cluster. Do not infer production readiness from offline tests.

## Platform safeguards

CloudNativePG and Barman chart versions remain pinned in `app.yml`. CNPG role
and database reclaim policies remain `retain`; PostgreSQL storage, backup,
network policy, and application connection protections remain in force. The
database NetworkPolicy permits broad TCP/443 egress because the Barman
sidecar shares the PostgreSQL instance Pod and must reach object storage; it does
not restrict HTTPS destinations and needs CNI-aware controls before a stricter
production boundary is claimed. Controller API access is limited to reviewed
K3s endpoint `/32`s on translated backend port 6443. The manager credential and
VSO trust bootstrap are supplied by the sibling Ansible repository. Never place generated
credentials in Git.
