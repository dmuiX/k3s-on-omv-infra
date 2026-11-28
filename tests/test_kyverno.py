"""Offline contracts for the Kyverno admission controller, policies, and metrics."""

import json
import unittest

from workload_contracts import Repository


class KyvernoContractTest(unittest.TestCase):
    def test_rendered_controllers_are_ha_hardened_and_bounded(self) -> None:
        repo = Repository()
        docs = repo.render("kyverno")

        deployments = {
            d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"
        }
        self.assertEqual(
            set(deployments),
            {
                "kyverno-admission-controller",
                "kyverno-background-controller",
                "kyverno-reports-controller",
            },
        )

        admission = deployments["kyverno-admission-controller"]
        self.assertEqual(admission["spec"]["replicas"], 2)
        admission_pod = admission["spec"]["template"]["spec"]
        anti_affinity = admission_pod["affinity"]["podAntiAffinity"][
            "preferredDuringSchedulingIgnoredDuringExecution"
        ]
        self.assertEqual(anti_affinity[0]["weight"], 100)
        self.assertEqual(
            anti_affinity[0]["podAffinityTerm"]["topologyKey"],
            "kubernetes.io/hostname",
        )

        for name, dep in deployments.items():
            pod_spec = dep["spec"]["template"]["spec"]
            self.assertEqual(
                pod_spec["nodeSelector"],
                {"compute-class": "standard", "kubernetes.io/os": "linux"},
                name,
            )
            for container in (
                *pod_spec.get("initContainers", []),
                *pod_spec["containers"],
            ):
                sec = container["securityContext"]
                self.assertIs(sec["runAsNonRoot"], True, name)
                self.assertIs(sec["readOnlyRootFilesystem"], True, name)
                self.assertIs(sec["allowPrivilegeEscalation"], False, name)
                self.assertEqual(sec["capabilities"]["drop"], ["ALL"], name)
                self.assertIn("cpu", container["resources"]["requests"], name)
                self.assertIn("memory", container["resources"]["requests"], name)
                self.assertIn("memory", container["resources"]["limits"], name)

        (pdb,) = [d for d in docs if d["kind"] == "PodDisruptionBudget"]
        self.assertEqual(pdb["metadata"]["name"], "kyverno-admission-controller")
        self.assertEqual(pdb["spec"]["minAvailable"], 1)

        config_cm = next(
            d["data"]
            for d in docs
            if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "kyverno"
        )
        webhooks = json.loads(config_cm["webhooks"])
        excluded = webhooks["namespaceSelector"]["matchExpressions"][0]["values"]
        for ns in ("kube-system", "istio-system", "istio-gateway"):
            self.assertIn(ns, excluded)

    def test_metrics_monitors_and_ambient_access_controls(self) -> None:
        repo = Repository()
        docs = repo.render("kyverno")

        monitors = {
            d["metadata"]["name"]: d for d in docs if d["kind"] == "ServiceMonitor"
        }
        self.assertEqual(
            set(monitors),
            {
                "kyverno-admission-controller",
                "kyverno-background-controller",
                "kyverno-reports-controller",
            },
        )

        (authz,) = [d for d in docs if d["kind"] == "AuthorizationPolicy"]
        self.assertEqual(authz["metadata"]["name"], "kyverno-metrics")
        self.assertEqual(authz["spec"]["action"], "DENY")
        self.assertEqual(
            authz["spec"]["rules"][0]["from"][0]["source"]["notPrincipals"],
            [
                "cluster.local/ns/kube-prometheus-stack/"
                "sa/kube-prometheus-stack-prometheus"
            ],
        )
        self.assertEqual(
            authz["spec"]["rules"][0]["to"][0]["operation"]["ports"],
            ["8000"],
        )

        netpols = {
            d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"
        }
        self.assertEqual(
            set(netpols),
            {"kyverno-metrics-ingress", "kyverno-admission-webhook-ingress"},
        )
        metrics_ports = {
            p["port"]
            for p in netpols["kyverno-metrics-ingress"]["spec"]["ingress"][0]["ports"]
        }
        self.assertEqual(metrics_ports, {8000, 15008})
        webhook_ports = {
            p["port"]
            for p in netpols["kyverno-admission-webhook-ingress"]["spec"]["ingress"][0][
                "ports"
            ]
        }
        self.assertEqual(webhook_ports, {9443})

    def test_baseline_supply_chain_policies_start_in_audit_mode(self) -> None:
        repo = Repository()
        docs = repo.render("kyverno")

        policies = {
            d["metadata"]["name"]: d for d in docs if d["kind"] == "ClusterPolicy"
        }
        self.assertEqual(
            set(policies),
            {
                "require-image-digest",
                "restrict-image-registries",
                "verify-kyverno-image-signatures",
            },
        )
        for name, policy in policies.items():
            self.assertEqual(
                policy["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"],
                "5",
                name,
            )
            self.assertEqual(
                policy["spec"]["validationFailureAction"],
                "Audit",
                name,
            )

        verify_policy = policies["verify-kyverno-image-signatures"]
        self.assertEqual(verify_policy["spec"]["failurePolicy"], "Ignore")
        verify_rule = verify_policy["spec"]["rules"][0]["verifyImages"][0]
        self.assertIs(verify_rule["mutateDigest"], False)
        self.assertIs(verify_rule["verifyDigest"], False)
