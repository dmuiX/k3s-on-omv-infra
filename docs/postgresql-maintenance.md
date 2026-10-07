# PostgreSQL maintenance, backup and recovery

These are live-cluster procedures for Crunchy PGO
`PostgresCluster/postgresql/platform-postgres`. Inspection commands are
read-only. Every mutation, failover, restore, or destructive cleanup requires
fresh operator approval and a recorded change window.

Local rendering or controller health is not evidence that backup, recovery, or
HA has been accepted.

## Routine inspection

```sh
kubectl -n postgresql get postgrescluster platform-postgres
kubectl -n postgresql get pods,pvc \
  -l postgres-operator.crunchydata.com/cluster=platform-postgres -o wide
kubectl -n postgresql get pods \
  -l postgres-operator.crunchydata.com/cluster=platform-postgres,postgres-operator.crunchydata.com/role=master
kubectl -n postgresql get jobs -l postgres-operator.crunchydata.com/cluster=platform-postgres
```

Confirm:

- PGO's `PostgresCluster` health is Healthy. During the preservation-first
  migration release, Argo may remain `OutOfSync` solely because legacy resources
  are pending prune; diagnose any other difference.
- exactly three PostgreSQL instance pods are Ready on three distinct nodes;
- one instance is Patroni leader and two are replicas;
- all PGDATA PVCs are Bound, encrypted Longhorn volumes;
- `status.pgbackrest.repos[name=repo1].stanzaCreated` is true;
- Prometheus is scraping all exporter targets;
- no PostgreSQL, replication, backup, WAL archive, or PVC alert is firing.

Do not print PGO user Secrets or the pgBackRest credential Secret.

## Manual full backup

A scheduled backup may be almost 24 hours away after first activation. With
explicit approval, request one full backup through PGO:

```sh
kubectl -n postgresql annotate postgrescluster platform-postgres \
  postgres-operator.crunchydata.com/pgbackrest-backup="manual-$(date -u +%Y%m%dT%H%M%SZ)" \
  --overwrite
```

The Git-owned `spec.backups.pgbackrest.manual` policy selects `repo1` and
`--type=full`; the annotation value is only a unique request identifier. Watch
the operator-created Job without dumping its environment:

```sh
kubectl -n postgresql get jobs,pods \
  -l postgres-operator.crunchydata.com/cluster=platform-postgres -w
```

Acceptance requires independent evidence that the Job completed, pgBackRest
can list the backup set in R2, and WAL archiving remains current. Controller
health alone is insufficient.

## Failover acceptance

Use disposable test data and a planned window.

1. Record the current leader, timeline, replica health, synchronous standby,
   and application connectivity.
2. Confirm a recent accepted pgBackRest backup and current WAL archive.
3. Delete only the current leader pod after explicit approval. Do not delete
   its PVC.
4. Measure promotion and application recovery through the stable
   `platform-postgres-primary.postgresql.svc` Service.
5. Confirm exactly one new leader, two healthy replicas, a synchronous standby,
   and no split brain.
6. Confirm the replaced instance rejoins without changing node separation.

Also test the deliberate degraded-mode policy in an isolated acceptance window:
with both standbys unavailable, the primary should remain writable because
`synchronous_mode_strict` is false. Record the acknowledged data-loss window;
do not leave the cluster degraded.

## Node maintenance

1. Verify backup/WAL health and three Ready instances.
2. Work on one node only.
3. Cordon and drain using the reviewed maintenance procedure. Respect the
   generated instance disruption budget; do not force past it.
4. Wait for Patroni and PGO to return to three Ready instances before touching
   another node.
5. Recheck synchronous replication, exporter targets, and applications.

Strict-local Longhorn data cannot move transparently to another node. If a node
or volume is permanently lost, preserve evidence, create a replacement
instance/PVC through a reviewed PGO change, and let PostgreSQL reconstruct it
from a healthy member. Never copy PGDATA files manually.

## Restore and PITR acceptance

Never restore over the production cluster. Create a separately named temporary
`PostgresCluster` using PGO's `dataSource.postgresCluster` restore configuration
and a reviewed recovery target. Use separate PVCs and Services.

Acceptance must demonstrate both:

- restore of the latest accepted backup; and
- point-in-time recovery to a timestamp between two recorded transactions.

Validate database ownership, application data, TLS connectivity, timelines,
and the requested recovery target. Delete the temporary cluster only after the
results and required evidence are retained. A production cutover requires its
own approved plan and must preserve the old cluster and R2 backup history until
independently verified.

## Storage expansion

Expand only by increasing the Git-owned request. Longhorn supports expansion,
but verify filesystem growth and all three instance PVCs individually. Never
shrink a PVC.

## Legacy CloudNativePG cleanup

PGO does not adopt CloudNativePG resources or PVCs. If legacy CNPG/Barman
objects exist, retain their PVCs, backups, and recovery material until PGO
application, failover, full-backup, restore, and PITR acceptance is complete.
Delete them only in a separate explicitly approved cleanup. Do not let Argo
pruning stand in for custody verification.
