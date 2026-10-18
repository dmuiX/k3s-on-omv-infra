import re
import unittest
from contracts import ROOT, app_files, text


class ReferenceIntegrityTests(unittest.TestCase):
    def test_all_local_value_files_exist(self):
        for app in app_files():
            for ref in re.findall(r"\$values/([^\s,\]}]+)", text(app)):
                self.assertTrue((ROOT / ref).is_file(), f"{app}: missing $values/{ref}")

    def test_all_infra_local_paths_exist(self):
        for app in app_files():
            content = text(app)
            for path in re.findall(r"\bpath:\s*([^\s,}\]]+)", content):
                if path.startswith("clusters/") or path == ".":
                    continue
                self.assertTrue((ROOT / path).exists(), f"{app}: missing path {path}")

    def test_directory_include_targets_exist(self):
        for app in app_files():
            content = text(app)
            if "k3s-on-omv-infra.git" not in content:
                continue
            for raw in re.findall(r"include:\s*[\"']?([^\n\"']+)", content):
                for item in raw.strip("{} ").split(","):
                    item = item.strip("{} []")
                    if not item or "*" in item or item.endswith("app.yml"):
                        continue
                    self.assertTrue((app.parent / item).is_file(), f"{app}: missing include target {item}")

    def test_kustomizations_reference_existing_files(self):
        for kust in ROOT.glob("0[1-8]-*/**/kustomization.yaml"):
            content = text(kust)
            for rel in re.findall(r"(?m)^\s{2}- ([^#\s]+\.ya?ml)$", content):
                self.assertTrue((kust.parent / rel).is_file(), f"{kust}: missing {rel}")

    def test_root_does_not_select_runtime_or_resource_manifests(self):
        include = text("infra.yml").split("include:", 1)[1].splitlines()[0]
        self.assertNotIn("recurse: true", include)
        self.assertNotIn("Lease", include)
        self.assertNotIn("clusters/", include)


if __name__ == "__main__":
    unittest.main()
