"""Offline contracts for the opt-in Tekton control plane and Envoy pilot."""

import json
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "01-bootstrap/istio-gateway/build"
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

    def test_pilot_requires_manual_run_and_an_explicit_builder(self) -> None:
        self.assertFalse(list(BUILD.rglob("app.yml")))
        self.assertFalse((ROOT / ".github/workflows/envoy-backport.yml").exists())
        gateway = yaml.safe_load((BUILD.parent / "app.yml").read_text())
        self.assertEqual(
            gateway["spec"]["sources"][0]["path"],
            "01-bootstrap/istio-gateway/resources",
        )
        manifests = list(yaml.safe_load_all((BUILD / "pipeline.yaml").read_text()))
        docs = {d["kind"]: d for d in manifests}
        self.assertEqual(len(docs), len(manifests))
        self.assertEqual(
            set(docs),
            {
                "Namespace",
                "ServiceAccount",
                "NetworkPolicy",
                "Task",
                "Pipeline",
            },
        )
        account = docs["ServiceAccount"]
        self.assertIs(account["automountServiceAccountToken"], False)
        self.assertNotIn("secrets", account)
        run = yaml.safe_load((BUILD / "run.yaml").read_text())
        template = run["spec"]["taskRunTemplate"]
        self.assertEqual(template["serviceAccountName"], account["metadata"]["name"])
        self.assertEqual(
            run["spec"]["pipelineRef"]["name"], docs["Pipeline"]["metadata"]["name"]
        )
        self.assertEqual(
            template["podTemplate"]["nodeSelector"],
            {
                "ci.dmuix.com/envoy-builder": "true",
                "kubernetes.io/arch": "amd64",
                "kubernetes.io/os": "linux",
            },
        )
        task = docs["Task"]["spec"]
        self.assertEqual(
            docs["Pipeline"]["spec"]["tasks"],
            [{"name": "build", "taskRef": {"name": docs["Task"]["metadata"]["name"]}}],
        )
        policy = docs["NetworkPolicy"]["spec"]
        self.assertEqual(set(policy["policyTypes"]), {"Ingress", "Egress"})
        self.assertNotIn("ingress", policy)
        self.assertEqual(
            {(p["protocol"], p["port"]) for r in policy["egress"] for p in r["ports"]},
            {("TCP", 53), ("UDP", 53), ("TCP", 443)},
        )
        volumes = {volume["name"]: volume for volume in task["volumes"]}
        self.assertEqual(set(volumes["build"]), {"name", "emptyDir"})
        self.assertEqual(volumes["inputs"]["configMap"], {"name": "envoy-build-inputs"})
        (step,) = task["steps"]
        self.assertIn(
            {"name": "inputs", "mountPath": "/inputs", "readOnly": True},
            step["volumeMounts"],
        )
        lock = json.loads((BUILD / "lock.json").read_text())
        self.assertEqual(step["image"], lock["toolchain"])
        self.assertNotIn("raw.githubusercontent.com", step["script"])
        for filename in ("lock.json", "48409.patch", "regression.patch", "inside.sh"):
            self.assertTrue((BUILD / filename).is_file())
            self.assertIn(filename, step["script"])
        self.assertIs(step["securityContext"]["allowPrivilegeEscalation"], False)
