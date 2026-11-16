"""Placement tuning must not take ownership of K3s DNS or resource metrics."""

from pathlib import Path
from typing import Any
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
