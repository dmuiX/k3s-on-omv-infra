# PostgreSQL Ad-Hoc Backups & Local-Mirror (OMV-Only) Recovery

This document defines the supported procedures for **ad-hoc CloudNativePG backups** and **local-mirror-only (OMV) recovery** when remote S3/R2 object storage is unavailable.

## 1. Automated Verification Scope

The daily `postgresql-restore-validation` CronJob (`04:00 Europe/Berlin`, `backup-validation` namespace) verifies three independent properties against a disposable `postgresql-restore-test` cluster (`longhorn-single-node-delete`):

1. **Point-in-time recovery (PITR)**: Restores to an exact `before`/`after` timestamp boundary and asserts only the `before` marker exists.
2. **Application database readability (`k3s-on-omv.io/app-databases-verified=true`)**: Connects read-only (`default_transaction_read_only=on`) to both restored application databases on the isolated cluster and asserts non-empty schema/data:
   - `grafana`: `migration_log`, `org`, `"user"`
   - `radar`: `radar_timeline_schema_migrations`, `radar_timeline_seen_resources`, `radar_timeline_events`
3. **Local mirror completeness (`k3s-on-omv.io/omv-mirror-verified=true`)**: Confirms every `DONE` Barman base backup in the remote catalog is present on the local NAS mirror (`/omv`).

> **Safety invariant:** `enableSuperuserAccess: true` is enabled **only** on the short-lived disposable `postgresql-restore-test` cluster in `backup-validation`, never on production `k3s-postgres`.

---

## 2. Ad-Hoc Backups

Use ad-hoc backups before major schema upgrades, PostgreSQL minor/major version changes, or storage migrations:

```bash
# Trigger an on-demand base backup via the Barman Cloud plugin
kubectl cnpg backup k3s-postgres \
  -n cloudnative-pg \
  --method plugin \
  --plugin-name barman-cloud.cloudnative-pg.io \
  --backup-name "k3s-postgres-adhoc-$(date -u +%Y%m%d-%H%M%S)"

# Watch until Phase=completed
kubectl -n cloudnative-pg get backups.postgresql.cnpg.io -w
```

### Interaction with Retention & Validation
- Ad-hoc backups are written to the same `k3s-postgres-backups` Barman `ObjectStore` and follow the same retention window once superseded by newer completed backups.
- `postgresql-restore-validation` continues to validate the latest completed base backup + WAL stream automatically; do not bypass scheduled validation after an ad-hoc backup.

---

## 3. Local-Mirror (OMV-Only) Recovery When Remote S3/R2 Is Unreachable

Every completed R2 Barman base backup and WAL archive is mirrored locally to the NAS (`postgresql-backup-mirror-validation` read-only mount / host path on the NAS). If remote S3/R2 is unavailable:

### Pre-Recovery Verification
1. Inspect the local mirror directory on the NAS to confirm the desired `base/<backup_id>/backup.info` and corresponding `wals/` segments exist and match the last verified `postgresql-restore-status` Lease.
2. **Never** restore directly over live `k3s-postgres` PVCs. Always restore into an **isolated** cluster (or offline PostgreSQL container) first.

### Recovery Path
1. **Option A — In-cluster isolated recovery via local S3-compatible endpoint (e.g., temporary MinIO/rclone S3 gateway serving the read-only NAS mirror)**:
   - Point a disposable CNPG `ObjectStore` + `Cluster` (`bootstrap.recovery.source`) at the local S3 endpoint serving the mirrored `k3s-postgres` prefix.
   - Verify `grafana` and `radar` tables read-only before promoting or dumping data.
2. **Option B — Offline host-level recovery using `barman-cloud-restore` / `tar` extraction**:
   - Extract the base backup tarball (`base/<backup_id>/data.tar.gz` + tablespaces if any) into a disposable data directory owned by `postgres:postgres` (`0700`).
   - Configure `restore_command` to copy WAL segments from the mirrored `wals/` directory (e.g., `gzip -dc /mirror/k3s-postgres/wals/%f.gz > %p`).
   - Create `recovery.signal`, start an isolated PostgreSQL 18.7 instance, confirm `pg_is_in_recovery()` transitions to `false`, and verify `grafana` and `radar` schemas before any cutover.

### Post-Recovery Acceptance Gate
Before pointing live `grafana` or `radar` workloads at a recovered cluster, verify:
- All 3 production instances reach `streaming` replication with `0 B` lag (`pg_stat_replication` + `pg_replication_slots`).
- `grafana` (`migration_log`, `org`, `"user"`) and `radar` (`radar_timeline_events`, `radar_timeline_seen_resources`) respond cleanly.
- A fresh base backup completes and `postgresql-restore-validation` returns `result=success`.
