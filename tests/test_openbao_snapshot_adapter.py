import copy
import unittest
from dataclasses import replace

from adapters.openbao_snapshot.snapshot import (
    RETENTION,
    Snapshot,
    SnapshotState,
    execute_plan,
    plan_retention,
)


PREFIXES = {
    "daily": "openbao/daily",
    "weekly": "openbao/weekly",
    "monthly": "openbao/monthly",
}


def snapshot(kind, snapshot_id, *, succeeded=True, verified=True, key_prefix=None):
    prefix = key_prefix or PREFIXES[kind]
    stem = f"{prefix}/{snapshot_id}"
    return Snapshot(kind, snapshot_id, stem + ".snap", stem + ".snap.sha256", succeeded, verified)


def state_with(records, *, current=None, required=("daily",), listed=None, prefixes=None):
    current = current or records[-1].snapshot_id
    keys = tuple(key for item in records for key in (item.snapshot_key, item.checksum_key))
    return SnapshotState(prefixes or PREFIXES, tuple(records), listed or keys, current, required)


class FakeClient:
    def __init__(self, state):
        self.state = state
        self.deleted = []

    def read_state(self):
        return self.state

    def delete_object(self, key):
        self.deleted.append(key)


class OpenBaoSnapshotAdapterTests(unittest.TestCase):
    def test_normal_count_retention_by_independent_prefix(self):
        daily = [snapshot("daily", f"202601{day:02d}T010000Z") for day in range(1, 10)]
        weekly = [snapshot("weekly", f"20250{month}01T010000Z") for month in range(1, 6)]
        monthly = [snapshot("monthly", f"20240{month}01T010000Z") for month in range(1, 8)]
        current = "20260109T010000Z"
        records = daily + weekly + monthly + [snapshot("weekly", current), snapshot("monthly", current)]
        plan = plan_retention(state_with(records, current=current, required=("daily", "weekly", "monthly")))
        self.assertTrue(plan.allowed)
        self.assertEqual(12, len(plan.deletions))  # six logical snapshots and their checksums
        self.assertIn("openbao/daily/20260101T010000Z.snap", plan.deletions)
        self.assertNotIn("openbao/daily/20260109T010000Z.snap", plan.deletions)

    def test_unknown_remote_object_blocks_all_deletion(self):
        records = [snapshot("daily", f"202601{day:02d}T010000Z") for day in range(1, 9)]
        state = state_with(records, listed=tuple(
            [key for item in records for key in (item.snapshot_key, item.checksum_key)] + ["openbao/daily/foreign.tmp"]
        ))
        self.assertFalse(plan_retention(state).allowed)

    def test_missing_checksum_object_blocks(self):
        records = [snapshot("daily", "20260101T010000Z")]
        state = state_with(records, listed=(records[0].snapshot_key,))
        self.assertIn("missing", plan_retention(state).blocked_reason)

    def test_failed_and_unverified_partial_uploads_block(self):
        for record in (
            snapshot("daily", "20260101T010000Z", succeeded=False),
            snapshot("daily", "20260101T010000Z", verified=False),
        ):
            with self.subTest(record=record):
                self.assertFalse(plan_retention(state_with([record])).allowed)

    def test_missing_required_sunday_upload_blocks(self):
        records = [snapshot("daily", "20260104T010000Z")]
        plan = plan_retention(state_with(records, required=("daily", "weekly")))
        self.assertIn("partial current", plan.blocked_reason)

    def test_foreign_or_manipulated_prefix_blocks(self):
        cases = [
            state_with([snapshot("daily", "20260101T010000Z", key_prefix="openbao/daily-evil")]),
            state_with([snapshot("daily", "20260101T010000Z")], prefixes={**PREFIXES, "daily": "openbao/../daily"}),
        ]
        for state in cases:
            with self.subTest(state=state):
                self.assertFalse(plan_retention(state).allowed)

    def test_ambiguous_duplicate_snapshot_blocks(self):
        item = snapshot("daily", "20260101T010000Z")
        self.assertFalse(plan_retention(state_with([item, item])).allowed)

    def test_execution_revalidates_and_only_deletes_exact_planned_keys(self):
        records = [snapshot("daily", f"202601{day:02d}T010000Z") for day in range(1, 9)]
        state = state_with(records)
        plan = plan_retention(state)
        client = FakeClient(state)
        result = execute_plan(plan, client)
        self.assertTrue(result.allowed)
        self.assertEqual(list(plan.deletions), client.deleted)

        changed = FakeClient(replace(state, listed_keys=state.listed_keys + ("openbao/daily/injected",)))
        result = execute_plan(plan, changed)
        self.assertFalse(result.allowed)
        self.assertEqual([], changed.deleted)


if __name__ == "__main__":
    unittest.main()
