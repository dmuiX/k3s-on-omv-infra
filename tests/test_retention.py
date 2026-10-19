import unittest
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

PATH = Path(__file__).parents[1] / "adapters/postgresql-size-retention/retention.py"
SPEC = spec_from_file_location("retention", PATH)
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 14, tzinfo=timezone.utc)
        self.backups = [MODULE.Backup(str(i), self.now - timedelta(days=i), 3 * 1024**3) for i in range(3)]

    def plan(self, backups=None, **kwargs):
        return MODULE.plan_dry_run(
            backups or self.backups,
            "success",
            self.now,
            now=self.now,
            acceptance=True,
            catalog_matches_storage=True,
            **kwargs,
        )

    def test_dry_run_then_unchanged_catalog_authorizes_exact_candidate(self):
        status, dry_run = self.plan()
        self.assertIn("dry-run", status)
        self.assertEqual(dry_run, ["barman-cloud-backup-delete", "--dry-run", "--backup-id", "2", "--minimum-redundancy", "2"])
        execute = MODULE.authorize_after_dry_run(self.backups, list(self.backups), dry_run, dry_run_succeeded=True)
        self.assertEqual(execute, ["barman-cloud-backup-delete", "--backup-id", "2", "--minimum-redundancy", "2"])

    def test_changed_catalog_or_failed_dry_run_never_authorizes_delete(self):
        _, dry_run = self.plan()
        self.assertEqual(MODULE.authorize_after_dry_run(self.backups, self.backups[:-1], dry_run, dry_run_succeeded=True), [])
        self.assertEqual(MODULE.authorize_after_dry_run(self.backups, self.backups, dry_run, dry_run_succeeded=False), [])

    def test_stale_lease_running_backup_and_minimum_are_closed(self):
        self.assertEqual(MODULE.plan_dry_run(self.backups, "success", self.now - timedelta(days=9), now=self.now, acceptance=True, catalog_matches_storage=True)[1], [])
        self.assertEqual(self.plan(backup_running=True)[1], [])
        self.assertIn("minimum redundancy", self.plan(self.backups[:2])[0])

    def test_catalog_mismatch_duplicate_and_archival_protection_are_closed(self):
        self.assertEqual(MODULE.plan_dry_run(self.backups, "success", self.now, now=self.now, acceptance=True, catalog_matches_storage=False)[1], [])
        duplicate = [self.backups[0], self.backups[0], self.backups[1]]
        self.assertEqual(self.plan(duplicate)[1], [])
        protected = [MODULE.Backup(b.backup_id, b.completed_at, b.base_bytes, archival_protected=True) for b in self.backups]
        self.assertEqual(self.plan(protected)[1], [])

    def test_post_delete_requires_only_candidate_removed_and_two_remaining(self):
        self.assertTrue(MODULE.verify_post_delete(self.backups, self.backups[:2], "2"))
        self.assertFalse(MODULE.verify_post_delete(self.backups, self.backups[:1], "2"))
        self.assertFalse(MODULE.verify_post_delete(self.backups, self.backups[1:], "2"))


if __name__ == "__main__":
    unittest.main()
