"""Negative controls for public render/monitoring interfaces, not snapshots."""

import copy
from pathlib import Path
import tempfile
from typing import Any
import unittest


class WorkloadContractBoundaries(unittest.TestCase):
    def test_public_sources_cannot_traverse_or_symlink_into_private_workspaces(
        self,
    ) -> None:
        from workload_contracts import public_path

        with tempfile.TemporaryDirectory(prefix="workload-boundary-") as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            outside = root.parent / "private.yml"
            outside.write_text("fixture: not-readable-through-source\n")
            (root / "redirect.yml").symlink_to(outside)
            for value in ("../private.yml", str(outside), "redirect.yml"):
                with self.assertRaises(ValueError):
                    public_path(root, value)
            (root / "resource.yaml").write_text("kind: Service\n")
            self.assertEqual(public_path(root, "resource.yaml"), root / "resource.yaml")

    def test_crd_operator_enum_is_a_string_and_python_yaml_tags_are_rejected(
        self,
    ) -> None:
        from workload_contracts import parse_documents
        import yaml

        result = parse_documents(
            'kind: CustomResourceDefinition\nmetadata: {name: fixture}\nspec: {enum: [=, "!="]}\n'
        )
        self.assertEqual(result[0]["spec"]["enum"], ["=", "!="])
        with self.assertRaises(yaml.YAMLError):
            parse_documents('!!python/object/apply:os.system ["not-executed"]')

    def test_monitor_requires_a_matching_service_namespace_selector_and_named_port(
        self,
    ) -> None:
        from workload_contracts import monitor_has_target

        service: dict[str, Any] = {
            "kind": "Service",
            "metadata": {
                "name": "controller",
                "namespace": "ops",
                "labels": {"app": "controller"},
            },
            "spec": {"ports": [{"name": "metrics", "port": 8080}]},
        }
        monitor: dict[str, Any] = {
            "kind": "ServiceMonitor",
            "metadata": {"name": "controller", "namespace": "ops"},
            "spec": {
                "selector": {"matchLabels": {"app": "controller"}},
                "endpoints": [{"port": "metrics"}],
            },
        }
        self.assertTrue(monitor_has_target(monitor, [service]))
        for mutation in ("port", "namespace", "selector", "empty"):
            bad = copy.deepcopy(monitor)
            if mutation == "port":
                bad["spec"]["endpoints"][0]["port"] = "missing"
            elif mutation == "namespace":
                bad["spec"]["namespaceSelector"] = {"matchNames": ["wrong"]}
            elif mutation == "selector":
                bad["spec"]["selector"]["matchLabels"]["app"] = "wrong"
            else:
                bad["spec"]["endpoints"] = []
            self.assertFalse(monitor_has_target(bad, [service]))


if __name__ == "__main__":
    unittest.main()
