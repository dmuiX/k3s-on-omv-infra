# Central PostgreSQL platform

`Application/postgresql` is the single Wave-6 owner for CloudNativePG, the
Barman Cloud plugin and the central `platform-postgres` cluster. The bootstrap
root keeps this Application excluded until the mandatory PKI installation action
removes the PostgreSQL exclusion. There is no second enable flag.

## Fixed release and topology

- CloudNativePG chart `0.29.1`, operator `1.30.1`.
- Barman Cloud plugin chart `0.8.1`, plugin `0.15.1`.
- PostgreSQL `18.6` system image on Debian Bookworm.
- Every runtime image is pinned to its multi-platform OCI index digest; the
  indexes contain Linux `amd64` and `arm64` manifests.
- Three instances, hard hostname anti-affinity and one `5Gi` encrypted
  `longhorn-postgres` RWO claim per instance.
- `longhorn-postgres` uses one strict-local Longhorn block replica. PostgreSQL
  streaming replication, not Longhorn replication, supplies database HA.
- Quorum synchronous replication is `ANY 1` with `dataDurability: preferred`.
  The current primary may continue asynchronously when both standbys are lost;
  the corresponding alert marks loss of the RPO-0 guarantee.

The permitted hostnames are `omv`, `wyse5070` and `raspi4`. Strict locality binds
the sole Longhorn replica to the scheduled database pod. Before productive data,
verify that each host's effective Longhorn default disk is the intended SSD;
local rendering cannot prove physical media or Longhorn placement.

## Credentials and access

Secret values are never stored here. OpenBao KV remains their source of truth,
and HashiCorp VSO must materialize ordinary namespace-local Kubernetes Secrets
before this Application is activated:

- `grafana-db-credentials` as `kubernetes.io/basic-auth`, with `username` and
  `password`, for `DatabaseRole/grafana`;
- `radar-db-credentials` as `kubernetes.io/basic-auth`, with `username` and
  `password`, for `DatabaseRole/radar`;
- `postgresql-r2-credentials` as `Opaque`, with `ACCESS_KEY_ID`,
  `ACCESS_SECRET_KEY` and `AWS_REGION`, for
  `ObjectStore/platform-postgres-backups`. Credentials come from OpenBao;
  VSO renders the non-secret region from private Git into the destination
  because the Barman API exposes it only through a Secret key selector.

The existing guarded Bootstrap role creates missing Grafana and Radar passwords
once in OpenBao with KV-v2 `cas=0`; CNPG does not generate or rotate them. VSO
owns the destination Secrets, and neither this Kustomization nor the private
values repository may contain `vault:` placeholders or credential values.

The R2 endpoint and dedicated PostgreSQL bucket are non-secret identifiers in the
private live values repository. PostgreSQL does not reuse K8up's bucket,
credentials or Restic repository.

Grafana has a 25-connection role/database budget: two planned replicas with at
most 10 pool connections each and five migration/maintenance connections. Radar
has a 12-connection budget: two planned replicas with at most five pool
connections each and two migration/maintenance connections. Their application
tickets must enforce those per-pod pool limits.

CloudNativePG 1.30.1 does not expose database-level `CONNECT` grants in the
`Database` API. Ordered `pg_hba` rules therefore allow each registered role only
its own database and reject that role for every other database. Native
`DatabaseRole` and `Database` resources still own role/database creation; no
imperative SQL bootstrap is used. Both resources retain data and roles when a
manifest is removed.

Only the stable `platform-postgres-rw.postgresql.svc` service is an application
write endpoint. There is no Gateway, Ingress, NodePort or LoadBalancer.

## Backup and recovery boundary

The Barman plugin archives WAL continuously to dedicated Cloudflare R2 storage;
`archive_timeout=5min` bounds WAL switching during quiet periods. A base backup
runs daily at `03:30`, the first backup starts immediately, and retention is 30
days. Every generated cluster object inherits `k8up.io/backup: "false"`, so K8up
must not back up PGDATA.

Argo health remains progressing until the cluster, first scheduled backup,
Barman recovery window, roles and databases are reconciled. Missing VSO-owned
Secrets therefore fail closed rather than silently creating alternate
credentials. This is a deployment gate, not proof of restore, failover, storage
placement, TLS or RPO/RTO. Follow
[`docs/postgresql-maintenance.md`](../../docs/postgresql-maintenance.md) for live
acceptance and maintenance.

## Local checks

```sh
ruby tests/verify-postgresql.rb
ruby tests/verify-bootstrap.rb
ruby tests/verify-charts.rb
```

These commands render manifests and pinned charts without contacting Kubernetes.
