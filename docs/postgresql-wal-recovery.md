# PostgreSQL WAL retention and archive-only recovery

Incident: 15–16 November 2026 (UTC). This is an incident record and a gated recovery runbook, not a periodic cleanup job.

## Symptoms and verified causes

The cluster uses CNPG 1.30.1, PostgreSQL 18.6, plugin-barman-cloud 0.15.1 and one encrypted strict-local Longhorn replica per PostgreSQL PVC. PostgreSQL provides database-level replication; a local Longhorn volume is not itself redundant.

Three distinct conditions occurred:

1. Longhorn blocked replica scheduling on the host below its global minimum-free-space threshold (previously 25%). Reclaiming host space allowed volume recovery. This did not prove filesystem corruption. The Live repository's `infra/longhorn/values.yml` now sets `storageMinimalAvailablePercentage: 15`; per-disk `storageReserved` was not changed.
2. PostgreSQL-1's 20Gi PVC filled with approximately 18Gi WAL versus 2.2Gi actual database files. Its persisted, inactive physical HA slot `_cnpg_k3s_postgres_2` retained WAL from `13/4214BA98`, exactly matching the oldest retained WAL segment. The slot file format/version and CRC32C were verified read-only; after startup SQL confirmed that position. The primary's corresponding slot had NULL `restart_lsn`. CNPG skips HA-slot synchronization when the primary's slot position is empty and does not remove the existing local slot just for that reason. Retention grew to roughly 23GiB before cleanup.
3. Both standbys were replaying archive WAL with no `pg_stat_wal_receiver` row and no matching streaming sender on the primary. Their replay positions advanced but were behind the primary. Pod readiness and `Cluster in healthy state` did not establish current replication or failover readiness.

The archive-status directory contained `.done` markers and no `.ready` markers at inspection time. A simple pending-archive backlog was not demonstrated. Archive-only recovery and stale slot retention are related but distinct from host free space and PVC size.

## Changes and recovery performed

- Live commit `c6498d8`: Longhorn minimum free space 25% -> 15%, cluster-wide.
- Infra commit `60da9d1`: desired PostgreSQL size 20Gi -> 40Gi per instance, with the existing contract assertion updated.
- CNPG 1.30.1 returned at the full-WAL-disk guard before PVC reconciliation, so changing the Cluster size alone did not resize existing PVCs. After approval, only PostgreSQL-1's PVC request was manually patched to the already Git-owned 40Gi. Longhorn expanded the filesystem; PostgreSQL-1 restarted automatically and CNPG then expanded PostgreSQL-2 and -3. All three PVC capacities were verified as 40Gi.
- After separate approval and fresh checks, only the inactive physical slot `_cnpg_k3s_postgres_2` on PostgreSQL-1 was dropped. PostgreSQL-1 was a standby, PostgreSQL-3 remained primary, and PostgreSQL-1 had no downstream sender connections. No other instance's slot, table data, or WAL file was manually removed. No checkpoint or restart was forced.
- After PostgreSQL's own cleanup, PostgreSQL-1 WAL dropped from 24,830,280,928 bytes (~23.1GiB) to 624MiB. Its filesystem changed from approximately 25GiB used to 3.1GiB used, leaving approximately 37GiB free on the 40Gi volume. Slot absence and free space were independently verified. This resolves that retention blockage, not the archive-to-streaming problem.
- Infra commit `e87d512` added `spec.postgresql.parameters.max_slot_wal_keep_size: 10GB` and this runbook. Argo subsequently reached Synced/Succeeded, and read-only `SHOW`/`current_setting` queries verified **10GB active on all three instances**.
- PostgreSQL-1 subsequently reached **streaming**, with its replay LSN equal to the primary's current LSN at inspection. The primary's corresponding slot was active and advancing. PostgreSQL-2 was still progressing through archive WAL, with no receiver; do not infer its completion from readiness.

## Longhorn snapshot cleanup and the encrypted TRIM blocker

After the slot cleanup, the mounted filesystem used approximately 3.2GiB, but Longhorn still reported approximately 32.49GiB allocated. These are different accounting layers, not 32GiB of live PostgreSQL tables. Two automatic expansion snapshots (27 October and 16 November) retained older blocks, including the former WAL backlog; deleted blocks in an encrypted thin volume also need discard propagation before the backing store can release them.

After explicit approval, deletion was requested through the Longhorn Snapshot CR API for **only** these two PostgreSQL-1 snapshots:

- `expand-21491613696-pvc-6548dde2-ecfa-4b00-a193-eced3b1f6fcf`
- `expand-42966450176-pvc-6548dde2-ecfa-4b00-a193-eced3b1f6fcf`

Longhorn marked both removed and completed its native purge/coalescing process (`state=complete`, progress 100%, no purge error). The October snapshot disappeared from the engine chain. Allocation fell from **34,889,916,416 bytes (32.49GiB) to 29,340,057,600 bytes (27.32GiB)**, reclaiming approximately **5.17GiB**. The November snapshot still exists marked removed immediately before `volume-head`; it was not physically purged. The volume remained healthy. Thus both historical rollback points were retired, but physical reclamation is only partial. A delete response is not proof that physical blocks have been reclaimed: finalizers and engine purge status must be checked, and allocation may temporarily grow while snapshot data is merged. Do not remove finalizers or replica files by hand.

A filesystem trim of the exact PostgreSQL-1 global mount failed with **`the discard operation is not supported`**. Read-only `lsblk -D` showed discard support on its underlying iSCSI disk (512-byte granularity, 4GiB maximum), but zero discard capability on the LUKS mapping. `cryptsetup status` showed no `allow_discards` flag. This identifies a discard-propagation blocker in the encrypted mapping, not a reason to delete live data. No encryption mapping options, global Longhorn trim settings, or keys were modified/read to bypass it. Enabling discard can reveal allocation/free-space patterns through encryption and needs a separately reviewed, explicitly approved configuration path. Physical-reclamation acceptance remains open until purge completion and the discard limitation are resolved; do not equate 37GiB free inside the PVC with host-level recovery.

The proposed GitOps/Bootstrap default and the exact, not-yet-executed live rollout are documented in [Encrypted Longhorn discard](longhorn-encrypted-discard.md). That proposal does not add a StorageClass or recreate volumes.

## WAL limit semantics

The 10GB setting applies to WAL retained by replication slots at checkpoint/restartpoint processing. It is not a hard total cap on `pg_wal`, not a timed deletion policy, and not a cure for broken replication. Archive failures, recovery needs and other retention settings can retain additional WAL. A lagging slot can become invalid after required WAL is removed; the standby then needs archived WAL or a controlled rebuild from a healthy primary. Do not assume CNPG will automatically repair every invalidated slot; see upstream issue #11646.

Keep monitoring disk space, slot validity, streaming state and replay progress. Do not automate unconditional slot deletion, reset the WAL directory, disable archiving globally, or repeatedly increase volume size as a substitute for diagnosing retention.

## Manual recovery gates

Re-read live state before every intervention; instance roles can change.

Read-only checks on the affected standby:

```sql
BEGIN READ ONLY;
SELECT pg_is_in_recovery(), pg_last_wal_replay_lsn(), pg_last_xact_replay_timestamp();
SELECT slot_name, slot_type, active, restart_lsn, wal_status
FROM pg_replication_slots;
SELECT application_name, state FROM pg_stat_replication;
SELECT status FROM pg_stat_wal_receiver;
SELECT pg_size_pretty(COALESCE(sum(size), 0)) AS wal_size FROM pg_ls_waldir();
SHOW max_slot_wal_keep_size;
COMMIT;
```

On the current primary independently inspect `pg_stat_replication` and `pg_replication_slots`. No connected downstream is necessary but not sufficient evidence: disconnected legitimate consumers must also be excluded from the topology review. Confirm the exact slot owner, role, HA topology, archive/backup recovery options and user approval before removing a slot. Never copy an instance number from this incident into a new intervention.

For this incident only, the authorized statement was run against PostgreSQL-1:

```sql
SELECT pg_drop_replication_slot(slot_name)
FROM pg_replication_slots
WHERE slot_name = '_cnpg_k3s_postgres_2'
  AND slot_type = 'physical'
  AND NOT active
  AND pg_is_in_recovery()
  AND NOT EXISTS (SELECT 1 FROM pg_stat_replication);
```

The guards are not a replacement for the topology review and are not a generic recurring-job policy. PostgreSQL also rejects dropping an active slot. Afterward verify slot absence/recreation and position, actual WAL bytes, filesystem free space, primary identity and ongoing replay on both standbys. A managed slot may be recreated later; judge its position and retention, not just its name.

For the full-disk resize deadlock, a separately approved patch of the affected PVC to the exact desired `Cluster.spec.storage.size` can unblock recovery. Never infer an unbounded capacity increase. Check StorageClass expansion support, node capacity, Longhorn status, PVC request/capacity and the mounted filesystem separately. PVCs cannot simply be shrunk afterward.

## Change validation and review

Baseline for the WAL-limit change: Infra `60da9d1`, freshly compared with GitHub `main`. Production YAML changes only `max_slot_wal_keep_size`; the existing HA/storage contract gains an assertion for `10GB`. Storage size, synchronous replication, slot synchronization, credentials, images, network policy and primary identity are not changed by this limit. Both new documentation and its link from `backup-contract.md` were reviewed.

The focused HA/storage contract was run red (missing setting), then green (1 test, 20 assertions). Kubernetes server-side dry-run accepts the manifest; the existing deprecated PodMonitor-field warning remains. PostgreSQL reports this parameter as `sighup`, so applying it does not inherently require a database restart. `git diff --check` passes.

Full-suite limitations remain explicit: Python 110 tests, 18 failures, 4 errors and 11 skips, with exactly the same named failures/errors as the unchanged baseline; full Ruby contracts retain 3 failures and 5 errors. These unrelated platform/fixture failures were not changed to force a green result. This is not a clean full-platform release approval. SQL query/index performance optimization and write-testing production data are not applicable to the slot-maintenance/configuration operation; only read-only system-view diagnostics and the specifically authorized slot removal were used.

Code and differential review classification: MEDIUM operational risk, affecting retention on three instances. A lagging consumer may lose access to required local WAL after a checkpoint and need archive recovery or rebuilding; the operator's invalidated-slot behavior is an explicit limitation. No arbitrary slot-deletion job, new privilege, authentication change or blanket archive bypass was introduced. The one-off slot drop was not added to any deploy hook. Slot removal was followed by fresh SQL and filesystem evidence of cleanup, not merely an exit code. Runtime activation of the limit and ongoing replication remain separate acceptance gates.

## Upstream fixes and December update reminder

Verified against GitHub on 16 November 2026:

- [Issue #9301](https://github.com/cloudnative-pg/cloudnative-pg/issues/9301): full-disk condition prevents configured resizing. [PR #11466](https://github.com/cloudnative-pg/cloudnative-pg/pull/11466), reconciling PVC resize before the disk-full guard, was still **open**. Do not assume the next release includes this separate fix.
- [Issue #10419](https://github.com/cloudnative-pg/cloudnative-pg/issues/10419): archive-only recovery / missing streaming and misleading healthy state. Similar symptoms do not establish that every reported mechanism applies to this cluster.
- [Issue #11808](https://github.com/cloudnative-pg/cloudnative-pg/issues/11808) and [PR #11868](https://github.com/cloudnative-pg/cloudnative-pg/pull/11868): bounded archive-recovery windows let standbys retry streaming. Merged into `main` on **12 November 2026**, labeled **do not backport**; absent from installed 1.30.1.
- [Issue #11646](https://github.com/cloudnative-pg/cloudnative-pg/issues/11646): invalidated HA-slot recreation is a separate risk when limiting retention.
- [Milestone 1.31.0](https://github.com/cloudnative-pg/cloudnative-pg/milestone/40) targeted **1 December 2026**; [support/release schedule](https://github.com/cloudnative-pg/cloudnative-pg/blob/main/docs/src/supported_releases.md) says approximately December. This is a plan, not a guaranteed release date. 1.30.1 was still the latest published stable release when checked.

**December follow-up:** recheck the actual 1.31.0 release notes and artifacts for #11868; check #11466 separately. Review upgrade compatibility, CRDs, PostgreSQL images and plugin compatibility, then upgrade through the existing GitOps chart/image pins after approval. Do not deploy the unsupported `cloudnative-pg-testing:main` snapshot merely because the fix is merged. No automatic updater or scheduled deployment was created by this incident.

After any upgrade, require fresh live evidence on both standbys: `pg_stat_wal_receiver.status = 'streaming'`, primary senders in `streaming`, HA slots active with advancing positions, replay caught up and WAL/free-space stable. Do not accept only `readyInstances: 3`. Backup success metadata is not a restore test; validate recovery separately on isolated data, never against production.
