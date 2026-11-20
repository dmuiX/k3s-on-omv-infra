"""Negative controls for public render/monitoring interfaces, not snapshots."""

import copy
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import yaml


class WorkloadContractBoundaries(unittest.TestCase):
    def test_public_sources_cannot_traverse_or_symlink_into_private_workspaces(
        self,
    ) -> None:
        from workload_contracts import public_path

        with tempfile.TemporaryDirectory(prefix="workload-boundary-") as directory:
            root = Path(directory).resolve() / "repo"
            root.mkdir()
            outside = root.parent / "private.yml"
            outside.write_text("fixture: not-readable-through-source\n")
            (root / "redirect.yml").symlink_to(outside)
            for value in ("../private.yml", str(outside), "redirect.yml"):
                with self.assertRaises(ValueError):
                    public_path(root, value)
            (root / "resource.yaml").write_text("kind: Service\n")
            self.assertEqual(public_path(root, "resource.yaml"), root / "resource.yaml")

    def test_sibling_kustomize_base_cannot_reference_private_resources(self) -> None:
        from workload_contracts import validate_kustomization

        with tempfile.TemporaryDirectory(prefix="workload-boundary-") as directory:
            root = Path(directory).resolve() / "repo"
            app, base = root / "app", root / "base"
            app.mkdir(parents=True)
            base.mkdir()
            (app / "kustomization.yaml").write_text("resources: [../base]")
            config = base / "kustomization.yaml"
            (base / "source").touch()
            config.write_text(
                "configMapGenerator: [{name: fixture, files: [key=source]}]"
            )
            validate_kustomization(root, app)
            config.write_text("resources: [../../private.yml]")
            with self.assertRaisesRegex(ValueError, "escapes repository"):
                validate_kustomization(root, app)

    def test_kustomize_rejects_unconfined_inputs_before_invoking_renderer(self) -> None:
        from workload_contracts import render_kustomization

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "repo"
            app, base = root / "app", root / "base"
            app.mkdir(parents=True)
            base.mkdir()
            (app / "kustomization.yaml").write_text("resources: [../base]")
            outside = root.parent / "outside.yaml"
            outside.touch()
            checkout = base / "nested-checkout"
            checkout.mkdir()
            (checkout / ".git").touch()
            (checkout / "input").touch()
            (base / "link").symlink_to(outside)
            config = base / "Kustomization"
            cases: list[dict[str, Any]] = [
                {key: ["link"]} for key in ("resources", "bases", "components")
            ]
            cases += [
                {"patches": [{"path": str(outside)}]},
                {"patches": [{"path": "link"}]},
                {"resources": ["https://example.invalid/repo?ref=main"]},
                {"resources": ["git@example.invalid:org/repo"]},
                {"resources": ["missing.yaml"]},
                {"resources": ["nested-checkout/input"]},
                {"resources": ["../app/kustomization.yaml"]},
                {"patches": [{"path": "../app"}]},
            ]
            for kind in ("configMapGenerator", "secretGenerator"):
                for source in (
                    {"files": ["link"]},
                    {"files": ["entry=link"]},
                    {"env": "link"},
                    {"envs": ["link"]},
                ):
                    cases.append({kind: [{"name": "fixture", **source}]})
            # Unsupported loaders are denied outright rather than trusting the tool's
            # default restrictions (some can read further files or fetch remote bases).
            for key in (
                "patchesJson6902",
                "patchesStrategicMerge",
                "replacements",
                "crds",
                "configurations",
                "generators",
                "transformers",
                "validators",
                "openapi",
                "helmCharts",
                "helmGlobals",
            ):
                cases.append({key: ["link"]})
            for name in (
                ".git",
                "private",
                "credentials",
                "secrets",
                "k3s-on-omv-live",
                "k3s-on-omv-apps",
                "k3s-on-omv-bootstrap",
            ):
                hidden = base / name
                hidden.mkdir()
                (hidden / "input").touch()
                alias = base / ("alias-" + name)
                alias.symlink_to(hidden, target_is_directory=True)
                for parent in (name, alias.name):
                    cases.append(
                        {
                            "configMapGenerator": [
                                {"name": "cm", "files": [f"key={parent}/input"]}
                            ]
                        }
                    )
            with patch("workload_contracts.subprocess.run") as run:
                for case in cases:
                    with self.subTest(case=case):
                        config.write_text(yaml.safe_dump(case))
                        with self.assertRaises(ValueError):
                            render_kustomization(root, app)
                        run.assert_not_called()
                config.unlink()
                config.symlink_to(outside)
                with self.assertRaises(ValueError):
                    render_kustomization(root, app)
                with self.assertRaises(ValueError):
                    render_kustomization(root, root.parent)
                run.assert_not_called()

    def test_crd_operator_enum_is_a_string_and_python_yaml_tags_are_rejected(
        self,
    ) -> None:
        from workload_contracts import parse_documents

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
