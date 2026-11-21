"""Offline contracts for the opt-in Tekton control plane and Envoy pilot."""

import hashlib
import unittest
from pathlib import Path

import yaml

from workload_contracts import render_kustomization

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "ci/tekton"
CONTROLLER = PILOT / "controller"


class TektonPilotTest(unittest.TestCase):
    def test_pinned_release_and_controller_overlay(self) -> None:
        # Tekton Pipelines v1.18.0 upstream release.yaml asset.
        self.assertEqual(
            hashlib.sha256((CONTROLLER / "release.yaml").read_bytes()).hexdigest(),
            "029c8400e42d0fcd4fc669140d382af1f1d8ae86202b710d6197ae22bd48be5f",
        )
        app_path = "02-controllers/tekton-pipelines/app.yml"
        root = yaml.safe_load((ROOT / "kustomization.yaml").read_text())
        self.assertIn(app_path, root["resources"])
        app = yaml.safe_load((ROOT / app_path).read_text())["spec"]
        self.assertEqual(app["sources"][0]["path"], "ci/tekton/controller")
        self.assertIs(app["syncPolicy"]["automated"]["enabled"], True)
        self.assertIs(app["syncPolicy"]["automated"]["prune"], False)
        self.assertIn("ServerSideApply=true", app["syncPolicy"]["syncOptions"])
        docs = render_kustomization(ROOT, CONTROLLER)
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
        self.assertFalse(list(PILOT.rglob("app.yml")))
        self.assertNotIn("ci/tekton", (ROOT / "kustomization.yaml").read_text())
        manifests = list(
            yaml.safe_load_all((PILOT / "pilot/pipeline.yaml").read_text())
        )
        docs = {d["kind"]: d for d in manifests}
        self.assertEqual(len(docs), len(manifests))
        self.assertEqual(
            set(docs),
            {
                "Namespace",
                "ServiceAccount",
                "ResourceQuota",
                "NetworkPolicy",
                "Task",
                "Pipeline",
            },
        )
        self.assertEqual(docs["ResourceQuota"]["spec"]["hard"]["pods"], "1")
        account = docs["ServiceAccount"]
        self.assertIs(account["automountServiceAccountToken"], False)
        self.assertNotIn("secrets", account)
        run = yaml.safe_load((PILOT / "pilot/run.yaml").read_text())
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
        (volume,) = task["volumes"]
        self.assertEqual(set(volume), {"name", "emptyDir"})
        (step,) = task["steps"]
        self.assertRegex(step["image"], r"@sha256:[0-9a-f]{64}$")
        self.assertRegex(step["script"], r"k3s-on-omv-infra/[a-f0-9]{40}/ci/envoy")
        self.assertIs(step["securityContext"]["allowPrivilegeEscalation"], False)
