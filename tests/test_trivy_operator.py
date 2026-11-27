"""Offline contracts for the Trivy Operator controller, server and metrics."""

import json
import unittest

from workload_contracts import Repository


class TrivyOperatorContractTest(unittest.TestCase):
    def test_rendered_operator_is_ha_hardened_and_bounded(self) -> None:
        repo = Repository()
        docs = repo.render("trivy-operator")

        (deployment,) = [d for d in docs if d["kind"] == "Deployment"]
        self.assertEqual(deployment["spec"]["replicas"], 2)
        pod_spec = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod_spec["nodeSelector"], {"compute-class": "standard"})
        affinity = pod_spec["affinity"]["podAntiAffinity"]
        anti_affinity = affinity["preferredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual(anti_affinity[0]["weight"], 100)
        self.assertEqual(
            anti_affinity[0]["podAffinityTerm"]["topologyKey"],
            "kubernetes.io/hostname",
        )
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

        (pdb,) = [d for d in docs if d["kind"] == "PodDisruptionBudget"]
        self.assertEqual(pdb["metadata"]["name"], "trivy-operator")
        self.assertEqual(pdb["spec"]["minAvailable"], 1)

        config = next(
            d["data"]
            for d in docs
            if d["kind"] == "ConfigMap"
            and d["metadata"]["name"] == "trivy-operator-config"
        )
        self.assertEqual(config["OPERATOR_LEADER_ELECTION_ENABLED"], "true")
        self.assertEqual(config["OPERATOR_VULNERABILITY_SCANNER_ENABLED"], "true")
        self.assertEqual(config["OPERATOR_EXPOSED_SECRET_SCANNER_ENABLED"], "true")
        self.assertEqual(config["OPERATOR_SBOM_GENERATION_ENABLED"], "true")
        self.assertEqual(config["OPERATOR_BUILT_IN_TRIVY_SERVER"], "true")
        self.assertEqual(config["OPERATOR_INFRA_ASSESSMENT_SCANNER_ENABLED"], "false")
        self.assertEqual(config["OPERATOR_CLUSTER_COMPLIANCE_ENABLED"], "false")
        self.assertEqual(config["OPERATOR_CONCURRENT_SCAN_JOBS_LIMIT"], "2")
        self.assertEqual(config["OPERATOR_SCAN_JOB_TIMEOUT"], "10m")
        self.assertEqual(config["OPERATOR_SCAN_JOB_TTL"], "24h")

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
        self.assertEqual(trivy_cm["trivy.mode"], "ClientServer")
        self.assertEqual(trivy_cm["trivy.slow"], "true")
        self.assertEqual(trivy_cm["trivy.timeout"], "10m0s")
        self.assertEqual(
            trivy_cm["trivy.additionalVulnerabilityReportFields"],
            "Description,Links,CVSS,Target,Class,PackagePath,PackageType",
        )
        self.assertEqual(trivy_cm["trivy.resources.requests.cpu"], "100m")
        self.assertEqual(trivy_cm["trivy.resources.requests.memory"], "128Mi")
        self.assertEqual(trivy_cm["trivy.resources.limits.cpu"], "500m")
        self.assertEqual(trivy_cm["trivy.resources.limits.memory"], "512Mi")

    def test_built_in_trivy_server_security_and_storage(self) -> None:
        repo = Repository()
        docs = repo.render("trivy-operator")

        (server,) = [d for d in docs if d["kind"] == "StatefulSet"]
        self.assertEqual(server["metadata"]["name"], "trivy-server")
        self.assertEqual(server["spec"]["replicas"], 1)
        (claim,) = server["spec"]["volumeClaimTemplates"]
        self.assertEqual(
            claim["spec"]["storageClassName"], "longhorn-single-node-delete"
        )
        self.assertEqual(claim["spec"]["resources"]["requests"]["storage"], "5Gi")

        pod_spec = server["spec"]["template"]["spec"]
        self.assertEqual(pod_spec["nodeSelector"], {"compute-class": "standard"})
        self.assertIs(pod_spec["automountServiceAccountToken"], False)
        self.assertEqual(
            pod_spec["securityContext"],
            {
                "runAsUser": 65534,
                "runAsGroup": 65534,
                "runAsNonRoot": True,
                "fsGroup": 65534,
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
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"},
            },
        )

    def test_metrics_monitor_crds_and_ambient_access_controls(self) -> None:
        repo = Repository()
        docs = repo.render("trivy-operator")

        crd_names = {
            d["metadata"]["name"]
            for d in docs
            if d["kind"] == "CustomResourceDefinition"
        }
        self.assertIn("vulnerabilityreports.aquasecurity.github.io", crd_names)
        self.assertIn("sbomreports.aquasecurity.github.io", crd_names)
        self.assertIn("configauditreports.aquasecurity.github.io", crd_names)
        self.assertIn("exposedsecretreports.aquasecurity.github.io", crd_names)

        (monitor,) = [d for d in docs if d["kind"] == "ServiceMonitor"]
        self.assertEqual(monitor["metadata"]["namespace"], "trivy-operator")
        self.assertEqual(monitor["spec"]["endpoints"][0]["port"], "metrics")

        authz = {
            d["metadata"]["name"]: d
            for d in docs
            if d["kind"] == "AuthorizationPolicy"
        }
        self.assertEqual(set(authz), {"trivy-operator-metrics", "trivy-server-api"})

        prometheus = (
            "cluster.local/ns/kube-prometheus-stack/"
            "sa/kube-prometheus-stack-prometheus"
        )
        metrics_policy = authz["trivy-operator-metrics"]
        self.assertEqual(metrics_policy["spec"]["action"], "DENY")
        self.assertEqual(
            metrics_policy["spec"]["rules"][0]["from"][0]["source"]["notPrincipals"],
            [prometheus],
        )
        self.assertEqual(
            metrics_policy["spec"]["rules"][0]["to"][0]["operation"]["ports"],
            ["8080"],
        )

        server_policy = authz["trivy-server-api"]
        self.assertEqual(server_policy["spec"]["action"], "DENY")
        self.assertEqual(
            server_policy["spec"]["rules"][0]["from"][0]["source"]["notPrincipals"],
            ["cluster.local/ns/trivy-operator/sa/trivy-operator"],
        )
        self.assertEqual(
            server_policy["spec"]["rules"][0]["to"][0]["operation"]["ports"],
            ["4954"],
        )

        netpols = {
            d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"
        }
        self.assertEqual(
            set(netpols),
            {"trivy-operator-metrics-ingress", "trivy-server-ingress"},
        )
        metrics_rule = netpols["trivy-operator-metrics-ingress"]["spec"]["ingress"][0]
        server_rule = netpols["trivy-server-ingress"]["spec"]["ingress"][0]
        self.assertEqual({p["port"] for p in metrics_rule["ports"]}, {8080, 15008})
        self.assertEqual({p["port"] for p in server_rule["ports"]}, {4954, 15008})
