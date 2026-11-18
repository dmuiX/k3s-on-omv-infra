"""Check the preview decision and the rendered read-only boundary."""

import copy
import runpy
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "06-platform/shutdown-pod-preview"
PREVIEW = runpy.run_path(str(BUNDLE / "preview.py"))
NOW = datetime(2026, 10, 1, 14, tzinfo=timezone.utc)
NAMESPACE = "argocd"
CORE = f"/api/v1/namespaces/{NAMESPACE}"
APPS = f"/apis/apps/v1/namespaces/{NAMESPACE}"


def owner(kind: str, uid: str) -> dict[str, object]:
    """Build a controlling owner reference for a synthetic API response."""
    return {"kind": kind, "uid": uid, "controller": True}


class ShutdownPodPreviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pod: dict[str, Any] = {
            "metadata": {
                "namespace": NAMESPACE,
                "name": "fixture-old-pod",
                "uid": "old-pod-uid",
                "ownerReferences": [owner("ReplicaSet", "old-rs-uid")],
            },
            "status": {
                "phase": "Failed",
                "reason": "Terminated",
                "message": "Pod was terminated in response to imminent node shutdown.",
                "containerStatuses": [
                    {"state": {"terminated": {"finishedAt": "2026-10-01T12:59:59Z"}}}
                ],
            },
        }
        self.rs: dict[str, Any] = {
            "metadata": {
                "namespace": NAMESPACE,
                "uid": "old-rs-uid",
                "ownerReferences": [owner("Deployment", "dep-uid")],
            }
        }
        self.dep: dict[str, Any] = {
            "metadata": {
                "name": "argocd-dex-server",
                "namespace": NAMESPACE,
                "uid": "dep-uid",
                "generation": 3,
            },
            "spec": {"replicas": 2},
            "status": {
                "observedGeneration": 3,
                "replicas": 2,
                "updatedReplicas": 2,
                "availableReplicas": 2,
            },
        }

    def candidates(self) -> list[dict[str, str]]:
        """Run the real selection against a small fake API inventory."""
        responses = {
            CORE + "/pods?fieldSelector=status.phase%3DFailed": [self.pod],
            APPS + "/replicasets": [self.rs],
            APPS + "/deployments": [self.dep],
        }

        def read(path: str) -> dict[str, object]:
            if path in responses:
                return {"items": responses[path]}
            self.assertTrue(path.endswith("/pods?fieldSelector=status.phase%3DFailed"))
            return {"items": []}

        candidates: list[dict[str, str]] = PREVIEW["preview"](read, now=NOW)
        return candidates

    def test_matches_a_recovered_allowlisted_deployment_by_owner_uid(self) -> None:
        self.assertEqual(
            self.candidates(),
            [
                {
                    "namespace": NAMESPACE,
                    "pod": "fixture-old-pod",
                    "uid": "old-pod-uid",
                    "deployment": "argocd-dex-server",
                }
            ],
        )

    def test_non_shutdown_recent_or_nonterminal_pods_are_excluded(self) -> None:
        initial = copy.deepcopy(self.pod)
        for reason, phase, message, ended in (
            (
                "OOMKilled",
                "Failed",
                initial["status"]["message"],
                "2026-10-01T12:35:00Z",
            ),
            ("Terminated", "Failed", "Container terminated", "2026-10-01T12:35:00Z"),
            (
                "Terminated",
                "Running",
                initial["status"]["message"],
                "2026-10-01T12:35:00Z",
            ),
            (
                "Terminated",
                "Failed",
                initial["status"]["message"],
                "2026-10-01T13:00:01Z",
            ),
            (
                "Terminated",
                "Failed",
                initial["status"]["message"],
                "2026-10-01T12:35:00",
            ),
            ("Terminated", "Failed", initial["status"]["message"], None),
        ):
            with self.subTest(reason=reason, phase=phase, message=message, ended=ended):
                self.pod = copy.deepcopy(initial)
                self.pod["status"].update(reason=reason, phase=phase, message=message)
                self.pod["status"]["containerStatuses"][0]["state"]["terminated"][
                    "finishedAt"
                ] = ended
                self.assertEqual(self.candidates(), [])

    def test_jobs_and_stale_owner_references_are_excluded(self) -> None:
        for ref in (
            owner("Job", "old-rs-uid"),
            owner("ReplicaSet", "recreated-rs-uid"),
        ):
            with self.subTest(owner=ref):
                self.pod["metadata"]["ownerReferences"] = [ref]
                self.assertEqual(self.candidates(), [])
        self.pod["metadata"]["ownerReferences"] = [owner("ReplicaSet", "old-rs-uid")]
        self.dep["metadata"]["uid"] = "recreated-dep-uid"
        self.assertEqual(self.candidates(), [])

    def test_incomplete_or_unapproved_deployments_are_excluded(self) -> None:
        initial = copy.deepcopy(self.dep)
        for field, value in (
            ("observedGeneration", 2),
            ("replicas", 3),
            ("updatedReplicas", 1),
            ("availableReplicas", 1),
        ):
            with self.subTest(field=field):
                self.dep["status"] = initial["status"] | {field: value}
                self.assertEqual(self.candidates(), [])
        self.dep = initial
        self.dep["metadata"]["name"] = "not-allowlisted"
        self.assertEqual(self.candidates(), [])

    def test_rendered_bundle_is_suspended_unregistered_and_has_no_write_access(
        self,
    ) -> None:
        render = subprocess.run(
            ["kustomize", "build", str(BUNDLE)],
            check=True,
            capture_output=True,
            text=True,
        )
        resources = list(yaml.safe_load_all(render.stdout))
        self.assertFalse((BUNDLE / "app.yml").exists())
        self.assertFalse(any(d["kind"] == "ClusterRoleBinding" for d in resources))
        (role,) = [d for d in resources if d["kind"] == "ClusterRole"]
        self.assertEqual(
            role["rules"],
            [
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
                {
                    "apiGroups": ["apps"],
                    "resources": ["deployments", "replicasets"],
                    "verbs": ["list"],
                },
            ],
        )
        bindings = [d for d in resources if d["kind"] == "RoleBinding"]
        self.assertEqual(len(bindings), len(PREVIEW["TARGETS"]))
        self.assertEqual(
            {d["metadata"]["namespace"] for d in bindings}, set(PREVIEW["TARGETS"])
        )
        for binding in bindings:
            self.assertEqual(binding["roleRef"]["name"], "shutdown-pod-preview")
            self.assertEqual(
                binding["subjects"],
                [
                    {
                        "kind": "ServiceAccount",
                        "name": "shutdown-pod-preview",
                        "namespace": "shutdown-pod-preview",
                    }
                ],
            )
        (cron,) = [d["spec"] for d in resources if d["kind"] == "CronJob"]
        self.assertIs(cron["suspend"], True)
        self.assertEqual(cron["concurrencyPolicy"], "Forbid")
        pod = cron["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "shutdown-pod-preview")
        (config,) = [d for d in resources if d["kind"] == "ConfigMap"]
        self.assertEqual(
            pod["volumes"][0]["configMap"]["name"], config["metadata"]["name"]
        )
        self.assertEqual(
            config["data"]["preview.py"], (BUNDLE / "preview.py").read_text()
        )
        (policy,) = [d["spec"] for d in resources if d["kind"] == "NetworkPolicy"]
        self.assertEqual(
            policy, {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
        )


if __name__ == "__main__":
    unittest.main()
