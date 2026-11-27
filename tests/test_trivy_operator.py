"""Offline contracts for the Trivy Operator controller, scan jobs and metrics."""

import json
import unittest

from workload_contracts import Repository


class TrivyOperatorContractTest(unittest.TestCase):
    def test_rendered_operator_is_hardened_and_bounded(self) -> None:
        repo = Repository()
        docs = repo.render("trivy-operator")

        (deployment,) = [d for d in docs if d["kind"] == "Deployment"]
        pod_spec = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod_spec["nodeSelector"], {"compute-class": "standard"})
        self.assertEqual(
            pod_spec["securityContext"],
            {
                "runAsNonRoot": True,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        )
        (container,) = pod_spec["containers"]
        self.assertEqual(
            container["securityContext"],
            {
                "privileged": False,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
        )
        self.assertEqual(
            container["resources"],
            {
                "requests": {"cpu": "20m", "memory": "128Mi"},
                "limits": {"cpu": "500m", "memory": "512Mi"},
            },
        )

        config = next(
            d["data"]
            for d in docs
            if d["kind"] == "ConfigMap"
            and d["metadata"]["name"] == "trivy-operator-config"
        )
        self.assertEqual(config["OPERATOR_VULNERABILITY_SCANNER_ENABLED"], "true")
        self.assertEqual(config["OPERATOR_EXPOSED_SECRET_SCANNER_ENABLED"], "true")
        self.assertEqual(config["OPERATOR_SBOM_GENERATION_ENABLED"], "false")
        self.assertEqual(config["OPERATOR_INFRA_ASSESSMENT_SCANNER_ENABLED"], "false")
        self.assertEqual(config["OPERATOR_CLUSTER_COMPLIANCE_ENABLED"], "false")
        self.assertEqual(config["OPERATOR_CONCURRENT_SCAN_JOBS_LIMIT"], "2")
        self.assertEqual(config["OPERATOR_SCAN_JOB_TTL"], "5m")

        operator_cm = next(
            d["data"]
            for d in docs
            if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "trivy-operator"
        )
        self.assertEqual(
            json.loads(operator_cm["scanJob.nodeSelector"]),
            {"compute-class": "standard"},
        )
        self.assertEqual(
            json.loads(operator_cm["scanJob.podTemplatePodSecurityContext"]),
            {
                "fsGroup": 10000,
                "runAsGroup": 10000,
                "runAsNonRoot": True,
                "runAsUser": 10000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        )
        self.assertEqual(
            json.loads(operator_cm["scanJob.podTemplateContainerSecurityContext"]),
            {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "privileged": False,
                "readOnlyRootFilesystem": True,
            },
        )

        trivy_cm = next(
            d["data"]
            for d in docs
            if d["kind"] == "ConfigMap"
            and d["metadata"]["name"] == "trivy-operator-trivy-config"
        )
        self.assertEqual(trivy_cm["trivy.slow"], "true")
        self.assertEqual(trivy_cm["trivy.resources.requests.cpu"], "100m")
        self.assertEqual(trivy_cm["trivy.resources.requests.memory"], "128Mi")
        self.assertEqual(trivy_cm["trivy.resources.limits.cpu"], "500m")
        self.assertEqual(trivy_cm["trivy.resources.limits.memory"], "512Mi")

    def test_metrics_monitor_crds_and_ambient_access_controls(self) -> None:
        repo = Repository()
        docs = repo.render("trivy-operator")

        crd_names = {
            d["metadata"]["name"]
            for d in docs
            if d["kind"] == "CustomResourceDefinition"
        }
        self.assertIn("vulnerabilityreports.aquasecurity.github.io", crd_names)
        self.assertIn("configauditreports.aquasecurity.github.io", crd_names)
        self.assertIn("exposedsecretreports.aquasecurity.github.io", crd_names)

        (monitor,) = [d for d in docs if d["kind"] == "ServiceMonitor"]
        self.assertEqual(monitor["metadata"]["namespace"], "trivy-operator")
        self.assertEqual(monitor["spec"]["endpoints"][0]["port"], "metrics")

        (authz,) = [d for d in docs if d["kind"] == "AuthorizationPolicy"]
        self.assertEqual(authz["spec"]["action"], "DENY")
        prometheus = (
            "cluster.local/ns/kube-prometheus-stack/"
            "sa/kube-prometheus-stack-prometheus"
        )
        self.assertEqual(
            authz["spec"]["rules"][0]["from"][0]["source"]["notPrincipals"],
            [prometheus],
        )
        self.assertEqual(
            authz["spec"]["rules"][0]["to"][0]["operation"]["ports"],
            ["8080"],
        )

        (netpol,) = [d for d in docs if d["kind"] == "NetworkPolicy"]
        self.assertEqual(netpol["spec"]["policyTypes"], ["Ingress"])
        self.assertEqual(
            {p["port"] for p in netpol["spec"]["ingress"][0]["ports"]},
            {8080, 15008},
        )
