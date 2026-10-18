import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contracts import ROOT, text


class AdapterContractTests(unittest.TestCase):
    def run_gate(self, registry):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blockers.json"
            path.write_text(json.dumps(registry))
            return subprocess.run(
                [sys.executable, str(ROOT / "tests/deployment_gate.py"), str(path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_gate_rejects_open_blocker(self):
        result = self.run_gate({
            "deploymentAllowed": False,
            "blockers": [{"id": "UNRESOLVED", "status": "open", "blocksDeployment": True}],
        })
        self.assertEqual(result.returncode, 2)
        self.assertIn("deployment blocked", result.stderr)

    def test_gate_accepts_fully_resolved_registry(self):
        result = self.run_gate({
            "deploymentAllowed": True,
            "blockers": [{"id": "DONE", "status": "resolved", "blocksDeployment": True}],
        })
        self.assertEqual(result.returncode, 0)

    def test_no_direct_production_s3_delete_is_authored_here(self):
        for path in ROOT.glob("0[1-8]-*/**/*"):
            if path.is_file():
                content = text(path)
                self.assertNotIn("aws s3 rm", content, str(path))
                self.assertNotIn("s3cmd del", content, str(path))


if __name__ == "__main__":
    unittest.main()
