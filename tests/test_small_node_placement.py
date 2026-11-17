"""Placement tuning must not take ownership of K3s DNS or resource metrics."""

from pathlib import Path
from typing import Any
import json
import os
import subprocess
import unittest

import yaml

INFRA = Path(__file__).resolve().parents[1]


def application(relative: str) -> dict[str, Any]:
    """Read one canonical public Application."""
    result: dict[str, Any] = yaml.safe_load((INFRA / relative / "app.yml").read_text())
    return result


class SmallNodePlacementTest(unittest.TestCase):
    def test_smb_moves_only_the_controller(self) -> None:
        values = application("02-controllers/csi-driver-smb")["spec"]["sources"][0][
            "helm"
        ]["valuesObject"]
        self.assertEqual(
            values["controller"]["nodeSelector"], {"compute-class": "standard"}
        )
        self.assertEqual(values["controller"]["replicas"], 1)
        self.assertNotIn("nodeSelector", values.get("node", {}))
        self.assertTrue(values["linux"]["enabled"])

    def test_longhorn_keeps_data_replicas_and_local_components(self) -> None:
        values = application("02-controllers/longhorn")["spec"]["sources"][0]["helm"][
            "valuesObject"
        ]
        self.assertEqual(values["csi"]["podAntiAffinityPreset"], "hard")
        for key in (
            "attacherReplicaCount",
            "provisionerReplicaCount",
            "resizerReplicaCount",
            "snapshotterReplicaCount",
        ):
            self.assertEqual(values["csi"][key], 3)
        self.assertNotIn(
            "systemManagedComponentsNodeSelector", values["defaultSettings"]
        )
        self.assertNotIn("nodeSelector", values["longhornManager"])
        self.assertEqual(
            values["defaultSettings"]["defaultReplicaCount"], '{"v1":"3","v2":"3"}'
        )
        self.assertEqual(
            values["defaultSettings"]["taintToleration"],
            "CriticalAddonsOnly=true:NoSchedule",
        )

    def test_ztunnel_only_reduces_memory_request(self) -> None:
        app = application("01-bootstrap/istio")
        (source,) = [s for s in app["spec"]["sources"] if s.get("chart") == "ztunnel"]
        self.assertEqual(
            source["helm"]["valuesObject"]["resources"],
            {
                "requests": {"cpu": "200m", "memory": "128Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"},
            },
        )

    def test_native_csi_requests_use_only_supported_components(self) -> None:
        values = application("02-controllers/longhorn")["spec"]["sources"][0]["helm"][
            "valuesObject"
        ]
        profile = json.loads(
            values["defaultSettings"]["systemManagedCSIComponentsResourceLimits"]
        )
        controllers = {
            "csi-attacher",
            "csi-provisioner",
            "csi-resizer",
            "csi-snapshotter",
        }
        self.assertEqual(
            set(profile),
            controllers
            | {
                "longhorn-csi-plugin",
                "node-driver-registrar",
                "longhorn-liveness-probe",
            },
        )
        for name in controllers:
            self.assertEqual(
                profile[name], {"requests": {"cpu": "10m", "memory": "80Mi"}}
            )
        for name, cpu in (
            ("longhorn-csi-plugin", "125m"),
            ("node-driver-registrar", "5m"),
            ("longhorn-liveness-probe", "5m"),
        ):
            self.assertEqual(
                profile[name], {"requests": {"cpu": cpu, "memory": "48Mi"}}
            )
        self.assertEqual(
            values["longhornGlobalManager"]["resources"],
            {"requests": {"cpu": "10m", "memory": "96Mi"}},
        )

    def test_request_profile_needs_the_paired_pi_budget_before_rollout(self) -> None:
        # Baseline includes PostgreSQL's restartable Barman init sidecar.
        values = application("02-controllers/longhorn")["spec"]["sources"][0]["helm"][
            "valuesObject"
        ]
        profile = json.loads(
            values["defaultSettings"]["systemManagedCSIComponentsResourceLimits"]
        )
        added = sum(
            int(c["requests"]["memory"].removesuffix("Mi")) for c in profile.values()
        )
        helper = next(
            document
            for document in yaml.safe_load_all(
                (
                    INFRA
                    / "06-platform/longhorn-config/resources/encrypted-discard.yaml"
                ).read_text()
            )
            if document.get("kind") == "DaemonSet"
        )
        pending_helper = int(
            helper["spec"]["template"]["spec"]["containers"][0]["resources"][
                "requests"
            ]["memory"].removesuffix("Mi")
        )
        self.assertEqual(pending_helper, 64)
        target = 1756 - 384 - 80 + 256 + pending_helper + added + (192 - 64)
        self.assertEqual(target, 2204)
        capacity = 3868328 / 1024
        self.assertLess(capacity - 1536 - 256 - 192 - target, 0)
        self.assertLess(capacity - 1024 - 256 - 192 - target, 128)
        self.assertGreaterEqual(capacity - 992 - 256 - 192 - target, 128)

    @unittest.skipUnless(
        os.environ.get("LONGHORN_CHART_ARCHIVE")
        or os.environ.get("PLATFORM_CHART_AUDIT"),
        "Pinned Longhorn chart is required",
    )
    def test_pinned_chart_consumes_native_resources_and_hard_affinity(self) -> None:
        values = application("02-controllers/longhorn")["spec"]["sources"][0]["helm"][
            "valuesObject"
        ]
        result = subprocess.run(
            [
                "helm",
                "template",
                "longhorn",
                os.environ.get("LONGHORN_CHART_ARCHIVE")
                or str(
                    Path(os.environ["PLATFORM_CHART_AUDIT"])
                    / "longhorn-1.13.0/longhorn"
                ),
                "--namespace",
                "longhorn",
                "--kube-version",
                "1.37.1",
                "-f",
                "-",
            ],
            input=yaml.safe_dump(values),
            text=True,
            capture_output=True,
            check=True,
            timeout=15,
        )
        docs = [d for d in yaml.safe_load_all(result.stdout) if d]
        (config,) = [
            d
            for d in docs
            if d["kind"] == "ConfigMap"
            and d["metadata"]["name"] == "longhorn-default-setting"
        ]
        native = yaml.safe_load(config["data"]["default-setting.yaml"])
        self.assertEqual(
            json.loads(native["system-managed-csi-components-resource-limits"]),
            json.loads(
                values["defaultSettings"]["systemManagedCSIComponentsResourceLimits"]
            ),
        )
        deployments = {
            d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"
        }
        global_container = deployments["longhorn-global-manager"]["spec"]["template"][
            "spec"
        ]["containers"][0]
        self.assertEqual(
            global_container["resources"], values["longhornGlobalManager"]["resources"]
        )
        env = deployments["longhorn-driver-deployer"]["spec"]["template"]["spec"][
            "containers"
        ][0]["env"]
        self.assertIn({"name": "CSI_POD_ANTI_AFFINITY_PRESET", "value": "hard"}, env)

    def test_dns_and_metrics_stay_out_of_the_gitops_root(self) -> None:
        paths = list(INFRA.glob("**/app.yml"))
        names = {yaml.safe_load(p.read_text())["metadata"]["name"] for p in paths}
        self.assertTrue({"coredns", "metrics-server"}.isdisjoint(names))
        root = yaml.safe_load((INFRA / "kustomization.yaml").read_text())
        self.assertEqual(
            set(root["resources"]), {str(p.relative_to(INFRA)) for p in paths}
        )
        self.assertFalse(
            (INFRA / "01-bootstrap/metrics-server/resources/api-service.yaml").exists()
        )


if __name__ == "__main__":
    unittest.main()
