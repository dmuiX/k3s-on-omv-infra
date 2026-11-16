"""The public CI selector must fail closed without importing private tests."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RepositorySuiteSelection(unittest.TestCase):
    def test_only_selected_tests_run_and_private_modules_are_not_imported(self) -> None:
        from repo_suite import load_repository_suite

        with tempfile.TemporaryDirectory(prefix="repo-suite-") as directory:
            root = Path(directory)
            (root / "test_ci_fixture_public.py").write_text(
                "import unittest\nclass Example(unittest.TestCase):\n"
                " def test_runs(self): self.assertEqual(2+2,4)\n"
            )
            (root / "test_ci_fixture_private.py").write_text(
                "raise AssertionError('private module must never be imported')\n"
            )
            (root / "suites.json").write_text(
                json.dumps(
                    {
                        "repository": ["test_ci_fixture_public"],
                        "mixed": {},
                        "integration": ["test_ci_fixture_private"],
                    }
                )
            )
            suite, deferred = load_repository_suite(root)
            self.assertEqual(suite.countTestCases(), 1)
            self.assertEqual(deferred, ["test_ci_fixture_private"])
            result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
            self.assertTrue(result.wasSuccessful())
            (root / "test_ci_fixture_unknown.py").write_text("# not classified\n")
            with self.assertRaisesRegex(ValueError, "Unclassified"):
                load_repository_suite(root)

    def run_entrypoint(self, body: str) -> subprocess.CompletedProcess[str]:
        """Exercise the shipped CLI itself with data-only synthetic test inputs."""
        with tempfile.TemporaryDirectory(prefix="repo-suite-entrypoint-") as directory:
            root = Path(directory)
            (root / "repo_suite.py").write_text(
                (Path(__file__).parent / "repo_suite.py").read_text()
            )
            (root / "test_cli_fixture.py").write_text(
                "import os, pathlib, unittest\nclass Example(unittest.TestCase):\n def test_case(self):\n"
                + "\n".join("  " + line for line in body.splitlines())
                + "\n"
            )
            (root / "suites.json").write_text(
                json.dumps(
                    {"repository": ["test_cli_fixture"], "mixed": {}, "integration": []}
                )
            )
            environment = {
                "PATH": os.environ.get("PATH", os.defpath),
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            for key in (
                "PLATFORM_LIVE_REPO",
                "PLATFORM_APPS_REPO",
                "PLATFORM_BOOTSTRAP_REPO",
                "KUBECONFIG",
                "VSO_TEMPLATE_SOURCE",
                "VSO_CHART_SOURCE",
                "VSO_CHART_DIR",
            ):
                environment[key] = "/must-not-be-read"
            environment.pop("PLATFORM_CHART_AUDIT", None)
            return subprocess.run(
                [sys.executable, str(root / "repo_suite.py")],
                env=environment,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
            )

    def test_cli_propagates_real_assertion_failure_and_rejects_mandatory_skip(
        self,
    ) -> None:
        for body, expected in (
            ("self.assertEqual(2+2, 4)", 0),
            ("self.fail('deliberate contract mutation')", 1),
            ("self.skipTest('missing mandatory tool')", 2),
        ):
            with self.subTest(expected_exit=expected):
                result = self.run_entrypoint(body)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_cli_forces_synthetic_inputs_and_discards_private_vso_overrides(
        self,
    ) -> None:
        result = self.run_entrypoint(
            "expected = str(pathlib.Path(__file__).parent / 'fixtures/public-values')\n"
            "for key in ('PLATFORM_LIVE_REPO','PLATFORM_APPS_REPO','PLATFORM_BOOTSTRAP_REPO'):\n"
            " self.assertEqual(os.environ[key], expected)\n"
            "self.assertEqual(os.environ['KUBECONFIG'], os.devnull)\n"
            "for key in ('VSO_TEMPLATE_SOURCE','VSO_CHART_SOURCE','VSO_CHART_DIR'):\n"
            " self.assertTrue(key not in os.environ, key + ' override was not discarded')"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_integration_overlap_and_nonexistent_selectors_are_rejected(self) -> None:
        from repo_suite import load_repository_suite

        with tempfile.TemporaryDirectory(prefix="repo-suite-") as directory:
            root = Path(directory)
            (root / "test_ci_fixture_case.py").write_text("# empty\n")
            path = root / "suites.json"
            path.write_text(
                json.dumps(
                    {
                        "repository": ["test_ci_fixture_case"],
                        "mixed": {},
                        "integration": ["test_ci_fixture_case"],
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "overlap"):
                load_repository_suite(root)
            path.write_text(
                json.dumps(
                    {
                        "repository": [],
                        "mixed": {"test_ci_fixture_case": ["Missing.test_missing"]},
                        "integration": [],
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "selector"):
                load_repository_suite(root)


if __name__ == "__main__":
    unittest.main()
