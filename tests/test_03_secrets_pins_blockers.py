import re
import unittest
from contracts import ROOT, app_files, app_name, blocker_annotations, blockers, text


class SecretsPinsBlockersTests(unittest.TestCase):
    def test_no_latest_or_unpinned_upstream_chart(self):
        for app in app_files():
            content = text(app)
            self.assertNotRegex(content, r"(?i)(targetRevision|tag):\s*[\"']?latest\b")
            if "chart:" in content:
                revisions = re.findall(r"targetRevision:\s*([^,\s}]+)", content)
                self.assertTrue(any(r not in {"main", "HEAD"} for r in revisions), app)
                if any(r.startswith("UNRESOLVED_") for r in revisions):
                    self.assertTrue(blocker_annotations(content), f"{app}: unresolved revision must block deployment")

    def test_no_obvious_secret_material(self):
        for path in ROOT.glob("0[1-8]-*/**/*"):
            if not path.is_file():
                continue
            content = text(path)
            self.assertNotRegex(content, r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
            self.assertNotRegex(content, r"(?i)\b(password|token|secret)\s*:\s*['\"]?[A-Za-z0-9+/]{24,}={0,2}\b")

    def test_blocker_registry_is_complete_and_deployment_blocking(self):
        registry = blockers()
        records = registry["blockers"]
        ids = {record["id"] for record in records}
        self.assertEqual(len(ids), len(records))
        open_records = [record for record in records if record["status"] != "resolved" and record["blocksDeployment"]]
        self.assertEqual(registry["deploymentAllowed"], not open_records)
        for record in records:
            self.assertIn(record["status"], {"open", "resolved"})
            self.assertIsInstance(record["blocksDeployment"], bool)
            self.assertTrue(record["requiredEvidence"].strip())
        annotated = {}
        for app in app_files():
            for blocker in blocker_annotations(text(app)):
                annotated.setdefault(blocker, set()).add(app_name(text(app)))
        declared = {
            record["id"]: set(record["applications"])
            for record in records
            if record["status"] != "resolved" and record["blocksDeployment"]
        }
        self.assertEqual(annotated, declared)

    def test_root_is_unsyncable_while_blockers_are_open(self):
        registry = blockers()
        root = text("infra.yml")
        open_records = [record for record in registry["blockers"] if record["status"] != "resolved" and record["blocksDeployment"]]
        expected_project = "infra-deployment-blocked" if open_records else "default"
        self.assertRegex(root, rf"(?m)^  project: {expected_project}$")


if __name__ == "__main__":
    unittest.main()
