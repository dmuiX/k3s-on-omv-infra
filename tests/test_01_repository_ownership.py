import unittest
from pathlib import Path
from contracts import CANONICAL, ROOT, app_files, app_name, app_wave, text


class RepositoryOwnershipTests(unittest.TestCase):
    def test_exact_canonical_inventory_paths_and_waves(self):
        files = app_files()
        records = [
            (app_name(text(path)), str(path.parent.relative_to(ROOT)), app_wave(text(path)))
            for path in files
        ]
        expected = sorted((name, location, wave) for name, (location, wave) in CANONICAL.items())
        self.assertEqual(sorted(records), expected)
        self.assertEqual(len(files), 21)
        self.assertEqual(len({record[0] for record in records}), 21)

    def test_root_selects_only_canonical_child_files(self):
        root = text("infra.yml")
        self.assertIn('include: "0[1-8]-*/*/app.yml"', root)
        self.assertNotIn("application-health.yaml", root)
        self.assertNotIn("redis", root)

    def test_apps_and_removed_webhook_are_not_in_infra_inventory(self):
        names = {app_name(text(path)) for path in app_files()}
        self.assertTrue({"radar", "audiobookshelf", "vault-secrets-webhook", "redis-operator"}.isdisjoint(names))
        for forbidden in ("07-applications", "08-applications", "04-secrets/vault-secrets-webhook"):
            self.assertFalse((ROOT / forbidden).exists())


if __name__ == "__main__":
    unittest.main()
