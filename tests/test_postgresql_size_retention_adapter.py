import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from adapters.postgresql_size_retention.retention import (
    BUDGET_BYTES,
    Backup,
    RestoreLease,
    RetentionState,
    StoredObject,
    barman_command,
    execute_plan,
    plan_retention,
)

UTC = timezone.utc
NOW = datetime(2026, 1, 10, tzinfo=UTC)
PREFIX = "production/k3s-postgres"
GIB = 1024**3


def backup(day, *, status="successful", archival=False):
    backup_id = f"202601{day:02d}T010000"
    completed = datetime(2026, 1, day, 2, tzinfo=UTC) if status == "successful" else None
    return Backup(backup_id, status, completed, archival)


def obj(item, size=2 * GIB, *, key_prefix=PREFIX):
    return StoredObject(f"{key_prefix}/base/{item.backup_id}/data.tar", size, "base", item.backup_id)


def valid_state(backups=None, objects=None, **changes):
    backups = tuple(backups or (backup(1), backup(2), backup(3)))
    objects = tuple(objects or (obj(item) for item in backups))
    values = dict(
        prefix=PREFIX,
        catalog_known=True,
        backups=backups,
        objects=objects,
        wal_archiver_stale=False,
        lease=RestoreLease("success", NOW - timedelta(days=1)),
        validation_accepted=True,
        now=NOW,
        budget_bytes=BUDGET_BYTES,
    )
    values.update(changes)
    return RetentionState(**values)


class Runner:
    def __init__(self, on_productive=None, statuses=None):
        self.calls = []
        self.on_productive = on_productive
        self.statuses = list(statuses or [])

    def run(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)
        if "--dry-run" not in argv and self.on_productive:
            self.on_productive()
        return self.statuses.pop(0) if self.statuses else 0


class Reader:
    def __init__(self, state):
        self.state = state
        self.reads = 0

    def read_state(self):
        self.reads += 1
        return self.state


class PostgreSQLSizeRetentionAdapterTests(unittest.TestCase):
    def test_over_budget_plans_exactly_one_oldest_backup(self):
        plan = plan_retention(valid_state())
        self.assertTrue(plan.needs_deletion)
        self.assertEqual("20260101T010000", plan.backup_id)

    def test_budget_boundary_counts_only_base_archives(self):
        backups = (backup(1), backup(2), backup(3))
        base = tuple(obj(item, BUDGET_BYTES // 3) for item in backups)
        # Bring the total exactly to budget and add arbitrarily large WAL/metadata.
        base = base[:-1] + (obj(backups[-1], BUDGET_BYTES - sum(o.size for o in base[:-1])),)
        extras = (
            StoredObject(PREFIX + "/wal/00000001", 20 * GIB, "wal"),
            StoredObject(PREFIX + "/metadata/catalog.json", 20 * GIB, "metadata"),
        )
        plan = plan_retention(valid_state(backups, base + extras))
        self.assertTrue(plan.allowed)
        self.assertFalse(plan.needs_deletion)
        self.assertEqual(BUDGET_BYTES, plan.total_base_bytes)

    def test_minimum_two_blocks_deletion_even_over_budget(self):
        backups = (backup(1), backup(2))
        plan = plan_retention(valid_state(backups, tuple(obj(item, 3 * GIB) for item in backups)))
        self.assertEqual("budget blocked by minimum redundancy", plan.blocked_reason)

    def test_unknown_catalog_and_catalog_statuses_block(self):
        self.assertFalse(plan_retention(valid_state(catalog_known=False)).allowed)
        self.assertFalse(plan_retention(valid_state((backup(1, status="mystery"),))).allowed)

    def test_running_backup_blocks(self):
        backups = (backup(1), backup(2), backup(3), backup(4, status="running"))
        objects = tuple(obj(item) for item in backups if item.status == "successful")
        self.assertEqual("backup running", plan_retention(valid_state(backups, objects)).blocked_reason)

    def test_stale_wal_blocks(self):
        self.assertEqual("stale WAL archiver", plan_retention(valid_state(wal_archiver_stale=True)).blocked_reason)

    def test_missing_failed_and_stale_lease_block(self):
        cases = (
            (None, "missing"),
            (RestoreLease("failed", NOW), "failed"),
            (RestoreLease("success", NOW - timedelta(days=8, seconds=1)), "stale"),
        )
        for lease, expected in cases:
            with self.subTest(lease=lease):
                self.assertIn(expected, plan_retention(valid_state(lease=lease)).blocked_reason)

    def test_missing_validation_acceptance_blocks(self):
        self.assertIn("not accepted", plan_retention(valid_state(validation_accepted=False)).blocked_reason)

    def test_unknown_missing_and_failed_object_mapping_blocks(self):
        backups = (backup(1), backup(2), backup(3))
        cases = (
            (obj(backups[0]), StoredObject(PREFIX + "/unknown/file", 1, "unknown")),
            (obj(backups[0]), obj(backups[1])),  # backup 3 archive is absent
            (obj(backups[0]), StoredObject(PREFIX + "/base/not-in-catalog/data", 1, "base", "20251201T010000")),
        )
        for objects in cases:
            with self.subTest(objects=objects):
                self.assertFalse(plan_retention(valid_state(backups, objects)).allowed)

    def test_foreign_and_manipulated_prefixes_block(self):
        backups = (backup(1), backup(2), backup(3))
        cases = (
            valid_state(prefix="production/../validation"),
            valid_state(backups, (obj(backups[0], key_prefix=PREFIX + "-evil"), obj(backups[1]), obj(backups[2]))),
        )
        for state in cases:
            with self.subTest(state=state):
                self.assertFalse(plan_retention(state).allowed)

    def test_archival_marked_backup_is_not_selected(self):
        backups = (backup(1, archival=True), backup(2), backup(3), backup(4))
        plan = plan_retention(valid_state(backups, tuple(obj(item) for item in backups)))
        self.assertEqual("20260102T010000", plan.backup_id)

    def test_dry_run_precedes_one_productive_barman_delete_and_post_reread(self):
        before = valid_state()
        remaining = before.backups[1:]
        after = valid_state(remaining, tuple(obj(item) for item in remaining))
        reader = Reader(before)

        def remove_via_barman():
            reader.state = after

        runner = Runner(on_productive=remove_via_barman)
        result = execute_plan(plan_retention(before), reader, runner)
        expected_id = "20260101T010000"
        self.assertEqual(barman_command(expected_id, dry_run=True), runner.calls[0])
        self.assertEqual(barman_command(expected_id, dry_run=False), runner.calls[1])
        self.assertEqual(2, len(runner.calls))
        self.assertGreaterEqual(reader.reads, 2)
        self.assertTrue(result.allowed)

    def test_failed_dry_run_prevents_productive_call(self):
        state = valid_state()
        runner = Runner(statuses=[1])
        result = execute_plan(plan_retention(state), Reader(state), runner)
        self.assertIn("dry-run failed", result.blocked_reason)
        self.assertEqual(1, len(runner.calls))

    def test_changed_catalog_prevents_all_commands(self):
        original = valid_state()
        changed = replace(original, objects=original.objects + (StoredObject(PREFIX + "/wal/new", 1, "wal"),))
        runner = Runner()
        result = execute_plan(plan_retention(original), Reader(changed), runner)
        self.assertIn("changed", result.blocked_reason)
        self.assertEqual([], runner.calls)

    def test_command_contains_only_id_and_redundancy_not_storage_secrets(self):
        command = barman_command("20260101T010000", dry_run=False)
        self.assertEqual(
            ("barman-cloud-backup-delete", "--backup-id", "20260101T010000", "--minimum-redundancy", "2"),
            command,
        )
        self.assertFalse(any("secret" in argument.lower() or "credential" in argument.lower() for argument in command))


if __name__ == "__main__":
    unittest.main()
