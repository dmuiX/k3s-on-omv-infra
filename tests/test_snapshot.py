import unittest
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

PATH = Path(__file__).parents[1] / "adapters/openbao-snapshot/snapshot.py"
SPEC = spec_from_file_location("snapshot", PATH)
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
DIGEST = "a" * 64


class SnapshotRetentionTest(unittest.TestCase):
    def test_keeps_newest_per_uploaded_prefix(self):
        keys = [f"daily/202601{i:02d}T000000Z.snap" for i in range(1, 10)]
        newest = keys[-1]
        plan = MODULE.deletion_plan(
            keys,
            {"daily": {"key": newest, "sha256": DIGEST}},
            required={"daily"},
        )
        self.assertEqual(plan, ["daily/20260101T000000Z.snap", "daily/20260102T000000Z.snap"])

    def test_month_start_and_sunday_require_all_independent_uploads(self):
        now = datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.assertEqual(MODULE.required_prefixes(now), set(MODULE.KEEP))
        keys = [f"{prefix}/2026{i:02d}01T000000Z.snap" for prefix in MODULE.KEEP for i in range(1, 9)]
        proofs = {prefix: {"key": f"{prefix}/20260801T000000Z.snap", "sha256": DIGEST} for prefix in MODULE.KEEP}
        self.assertEqual(len(MODULE.deletion_plan(keys, proofs, required=set(MODULE.KEEP))), 7)

    def test_rejects_partial_upload_bad_checksum_unknown_and_unlisted_proof(self):
        with self.assertRaises(ValueError):
            MODULE.deletion_plan([], {"daily": {"key": "daily/x.snap", "sha256": DIGEST}}, required={"daily", "weekly"})
        with self.assertRaises(ValueError):
            MODULE.deletion_plan(["daily/20260101T000000Z.snap"], {"daily": {"key": "daily/20260101T000000Z.snap", "sha256": "bad"}}, required={"daily"})
        with self.assertRaises(ValueError):
            MODULE.deletion_plan(["daily/latest.snap"], {"daily": {"key": "daily/latest.snap", "sha256": DIGEST}}, required={"daily"})
        with self.assertRaises(ValueError):
            MODULE.deletion_plan(["daily/20260101T000000Z.snap"], {"daily": {"key": "daily/20260102T000000Z.snap", "sha256": DIGEST}}, required={"daily"})


if __name__ == "__main__":
    unittest.main()
