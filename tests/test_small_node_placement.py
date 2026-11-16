"""Regression contracts for central workloads and bounded add-on adoption."""

from pathlib import Path
from typing import Any
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
    def test_coredns_preserves_legacy_selector_service_and_configuration(self) -> None:
        app = application("01-bootstrap/coredns")
        source = app["spec"]["sources"][0]
        self.assertEqual(source["chart"], "coredns")
        self.assertEqual(source["targetRevision"], "1.50.7")
        values = source["helm"]["valuesObject"]
        self.assertEqual(values["nodeSelector"], {"compute-class": "standard"})
        self.assertEqual(values["fullnameOverride"], "coredns")
        self.assertEqual(
            values["deployment"]["selector"], {"matchLabels": {"k8s-app": "kube-dns"}}
        )
        self.assertTrue(values["deployment"]["skipConfig"])
        self.assertEqual(values["service"]["name"], "kube-dns")
        self.assertEqual(values["service"]["selector"], {"k8s-app": "kube-dns"})
        self.assertNotIn("clusterIP", values["service"])
        self.assertEqual(values["k8sAppLabelOverride"], "kube-dns")
        self.assertEqual(
            values["zoneFiles"], [{"filename": "NodeHosts", "contents": ""}]
        )
        self.assertEqual(values["rollingUpdate"], {"maxSurge": 1, "maxUnavailable": 0})
        self.assertEqual(values["image"]["tag"], "1.14.7")
        self.assertFalse(app["spec"]["syncPolicy"]["automated"]["prune"])

    def test_metrics_backend_precedes_api_service_cutover(self) -> None:
        app = application("01-bootstrap/metrics-server")
        source = app["spec"]["sources"][0]
        self.assertEqual(source["chart"], "metrics-server")
        self.assertEqual(source["targetRevision"], "3.14.0")
        values = source["helm"]["valuesObject"]
        self.assertEqual(values["fullnameOverride"], "metrics-server-native")
        self.assertEqual(values["nodeSelector"], {"compute-class": "standard"})
        self.assertFalse(values["apiService"]["create"])
        self.assertNotIn("--kubelet-insecure-tls", values.get("args", []))
        api = yaml.safe_load(
            (
                INFRA / "01-bootstrap/metrics-server/resources/api-service.yaml"
            ).read_text()
        )
        self.assertEqual(api["metadata"]["name"], "v1beta1.metrics.k8s.io")
        self.assertEqual(
            api["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "1"
        )
        self.assertEqual(api["spec"]["service"]["name"], "metrics-server-native")
        self.assertEqual(api["spec"]["service"]["namespace"], "kube-system")
        self.assertEqual(
            app["spec"]["sources"][1]["path"], "01-bootstrap/metrics-server/resources"
        )

    def test_supported_placement_changes_leave_data_replicas_unchanged(self) -> None:
        smb = application("02-controllers/csi-driver-smb")["spec"]["sources"][0][
            "helm"
        ]["valuesObject"]
        self.assertEqual(
            smb["controller"]["nodeSelector"], {"compute-class": "standard"}
        )
        self.assertNotIn("nodeSelector", smb.get("node", {}))
        longhorn = application("02-controllers/longhorn")["spec"]["sources"][0]["helm"][
            "valuesObject"
        ]
        self.assertEqual(longhorn["csi"]["podAntiAffinityPreset"], "hard")
        for key in (
            "attacherReplicaCount",
            "provisionerReplicaCount",
            "resizerReplicaCount",
            "snapshotterReplicaCount",
        ):
            self.assertEqual(longhorn["csi"][key], 3)
        self.assertNotIn(
            "systemManagedComponentsNodeSelector", longhorn["defaultSettings"]
        )
        self.assertEqual(
            longhorn["defaultSettings"]["defaultReplicaCount"], '{"v1":"3","v2":"3"}'
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

    @unittest.skipUnless(
        os.environ.get("SYSTEM_ADDON_CHART_CACHE"), "Pinned chart archives are required"
    )
    def test_real_charts_preserve_dns_and_separate_metrics_cutover(self) -> None:
        cache = Path(os.environ["SYSTEM_ADDON_CHART_CACHE"])
        for name in ("coredns", "metrics-server"):
            with self.subTest(chart=name):
                source = application(f"01-bootstrap/{name}")["spec"]["sources"][0]
                result = subprocess.run(
                    [
                        "helm",
                        "template",
                        source["helm"]["releaseName"],
                        str(cache / f"{name}.tgz"),
                        "--namespace",
                        "kube-system",
                        "--kube-version",
                        "1.37.1",
                        "--values",
                        "-",
                    ],
                    input=yaml.safe_dump(source["helm"]["valuesObject"]),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=True,
                )
                docs = [d for d in yaml.safe_load_all(result.stdout) if d]
                (deployment,) = [d for d in docs if d["kind"] == "Deployment"]
                pod = deployment["spec"]["template"]["spec"]
                self.assertEqual(pod["nodeSelector"], {"compute-class": "standard"})
                for container in pod["containers"]:
                    self.assertIn("livenessProbe", container)
                    self.assertIn("readinessProbe", container)
                    self.assertFalse(
                        container["securityContext"]["allowPrivilegeEscalation"]
                    )
                    self.assertIn("memory", container["resources"]["limits"])
                if name == "coredns":
                    self.assertFalse(any(d["kind"] == "ConfigMap" for d in docs))
                    (service,) = [d for d in docs if d["kind"] == "Service"]
                    self.assertEqual(service["metadata"]["name"], "kube-dns")
                    self.assertEqual(
                        service["spec"]["selector"], {"k8s-app": "kube-dns"}
                    )
                    self.assertEqual(
                        {(p["port"], p["protocol"]) for p in service["spec"]["ports"]},
                        {(53, "TCP"), (53, "UDP")},
                    )
                    self.assertEqual(
                        deployment["spec"]["selector"],
                        {"matchLabels": {"k8s-app": "kube-dns"}},
                    )
                    (config,) = [
                        v for v in pod["volumes"] if v["name"] == "config-volume"
                    ]
                    self.assertEqual(
                        {i["key"] for i in config["configMap"]["items"]},
                        {"Corefile", "NodeHosts"},
                    )
                else:
                    self.assertEqual(
                        deployment["metadata"]["name"], "metrics-server-native"
                    )
                    self.assertFalse(any(d["kind"] == "APIService" for d in docs))
                    self.assertNotIn(
                        "--kubelet-insecure-tls", pod["containers"][0]["args"]
                    )

    def test_canonical_kustomization_includes_both_addons(self) -> None:
        root = yaml.safe_load((INFRA / "kustomization.yaml").read_text())
        for name in ("coredns", "metrics-server"):
            self.assertIn(f"01-bootstrap/{name}/app.yml", root["resources"])


if __name__ == "__main__":
    unittest.main()
