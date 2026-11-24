"""Offline contracts for the Tekton Pipelines controller and CRD hygiene."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "02-controllers/tekton-pipelines/resources"


class TektonPilotTest(unittest.TestCase):
    def test_controller_application_and_manifests(self) -> None:
        app_path = "02-controllers/tekton-pipelines/app.yml"
        root = yaml.safe_load((ROOT / "kustomization.yaml").read_text())
        self.assertIn(app_path, root["resources"])
        app = yaml.safe_load((ROOT / app_path).read_text())["spec"]
        self.assertEqual(
            app["sources"][0]["path"], CONTROLLER.relative_to(ROOT).as_posix()
        )
        self.assertIs(app["syncPolicy"]["automated"]["enabled"], True)
        self.assertIs(app["syncPolicy"]["automated"]["prune"], False)
        self.assertIn("ServerSideApply=true", app["syncPolicy"]["syncOptions"])
        docs = [
            d
            for d in yaml.safe_load_all((CONTROLLER / "release.yaml").read_text())
            if d is not None
        ]
        self.assertFalse(
            any(
                d["kind"] == "ClusterRole"
                and d["metadata"]["name"].startswith("tekton-aggregate-")
                for d in docs
            )
        )
        deployments = [d for d in docs if d["kind"] == "Deployment"]
        self.assertEqual(len(deployments), 4)
        builds = yaml.safe_load((CONTROLLER / "ci-builds.yaml").read_text())
        self.assertEqual(builds["kind"], "Namespace")
        self.assertEqual(builds["metadata"]["name"], "ci-builds")
        self.assertEqual(
            builds["metadata"]["labels"]["pod-security.kubernetes.io/enforce"],
            "restricted",
        )
        for deployment in deployments:
            spec = deployment["spec"]["template"]["spec"]
            self.assertEqual(
                spec["nodeSelector"],
                {"kubernetes.io/arch": "amd64", "kubernetes.io/os": "linux"},
            )
            for container in spec["containers"]:
                self.assertRegex(container["image"], r"@sha256:[0-9a-f]{64}$")
                for field in ("requests", "limits"):
                    self.assertEqual(
                        set(container["resources"][field]), {"cpu", "memory"}
                    )
                security = container["securityContext"]
                self.assertIs(security["allowPrivilegeEscalation"], False)
                self.assertIn("ALL", security["capabilities"]["drop"])
        flags = {
            d["metadata"]["name"]: d["data"]
            for d in docs
            if d["kind"] == "ConfigMap"
            and d["metadata"]["name"] in {"feature-flags", "resolvers-feature-flags"}
        }
        self.assertEqual(flags["feature-flags"]["disable-creds-init"], "true")
        self.assertEqual(
            flags["resolvers-feature-flags"],
            {
                f"enable-{resolver}-resolver": "false"
                for resolver in ("bundles", "hub", "git", "cluster", "http")
            },
        )

    def test_tekton_crds_omit_the_api_default_for_unknown_fields(self) -> None:
        docs = yaml.safe_load_all((CONTROLLER / "release.yaml").read_text())
        crds = [d for d in docs if d and d["kind"] == "CustomResourceDefinition"]
        self.assertTrue(crds)
        for crd in crds:
            with self.subTest(crd=crd["metadata"]["name"]):
                self.assertNotIn("preserveUnknownFields", crd["spec"])

    def test_envoy_backport_pipeline_is_retired(self) -> None:
        self.assertFalse((ROOT / "01-bootstrap/istio-gateway/build").exists())
        self.assertFalse((ROOT / ".github/workflows/envoy-backport.yml").exists())
        gateway = yaml.safe_load(
            (ROOT / "01-bootstrap/istio-gateway/app.yml").read_text()
        )
        self.assertEqual(
            gateway["spec"]["sources"][0]["path"],
            "01-bootstrap/istio-gateway/resources",
        )
