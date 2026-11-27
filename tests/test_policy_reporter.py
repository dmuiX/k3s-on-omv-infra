"""Offline contracts for Policy Reporter, UI, plugins, OIDC boundary, and mesh."""

import unittest

from workload_contracts import Repository


class PolicyReporterContractTest(unittest.TestCase):
    def test_rendered_workloads_are_hardened_and_bounded(self) -> None:
        repo = Repository()
        docs = repo.render("policy-reporter")

        deployments = {
            d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"
        }
        self.assertEqual(
            set(deployments),
            {
                "policy-reporter",
                "policy-reporter-ui",
                "policy-reporter-kyverno-plugin",
                "policy-reporter-trivy-plugin",
                "policy-reporter-oidc-proxy",
            },
        )

        for name in (
            "policy-reporter",
            "policy-reporter-ui",
            "policy-reporter-kyverno-plugin",
            "policy-reporter-trivy-plugin",
        ):
            pod_spec = deployments[name]["spec"]["template"]["spec"]
            self.assertEqual(pod_spec["nodeSelector"], {"compute-class": "standard"})
            (container,) = pod_spec["containers"]
            sec = container["securityContext"]
            self.assertIs(sec["runAsNonRoot"], True, name)
            self.assertIs(sec["readOnlyRootFilesystem"], True, name)
            self.assertIs(sec["allowPrivilegeEscalation"], False, name)
            self.assertEqual(sec["capabilities"]["drop"], ["ALL"], name)
            self.assertIn("cpu", container["resources"]["requests"], name)
            self.assertIn("memory", container["resources"]["requests"], name)
            self.assertIn("memory", container["resources"]["limits"], name)
            self.assertNotIn("cpu", container["resources"]["limits"], name)

    def test_route_oidc_and_ambient_access_controls(self) -> None:
        repo = Repository()
        docs = repo.render("policy-reporter")

        (route,) = [d for d in docs if d["kind"] == "HTTPRoute"]
        self.assertEqual(route["metadata"]["name"], "policy-reporter")
        self.assertEqual(route["metadata"]["namespace"], "kyverno")
        (rule,) = route["spec"]["rules"]
        self.assertEqual(
            rule["backendRefs"],
            [
                {
                    "group": "",
                    "kind": "Service",
                    "name": "policy-reporter-oidc-proxy",
                    "port": 80,
                    "weight": 1,
                }
            ],
        )

        (monitor,) = [d for d in docs if d["kind"] == "ServiceMonitor"]
        self.assertEqual(monitor["metadata"]["name"], "policy-reporter-monitoring")

        authz = {
            d["metadata"]["name"]: d for d in docs if d["kind"] == "AuthorizationPolicy"
        }
        self.assertEqual(
            set(authz),
            {"policy-reporter-ui", "policy-reporter-api", "policy-reporter-plugins"},
        )
        self.assertEqual(
            authz["policy-reporter-ui"]["spec"]["rules"][0]["from"][0]["source"][
                "notPrincipals"
            ],
            ["cluster.local/ns/kyverno/sa/policy-reporter-oidc-proxy"],
        )
        self.assertEqual(
            set(
                authz["policy-reporter-api"]["spec"]["rules"][0]["from"][0]["source"][
                    "notPrincipals"
                ]
            ),
            {
                "cluster.local/ns/kyverno/sa/policy-reporter-ui",
                "cluster.local/ns/kube-prometheus-stack/"
                "sa/kube-prometheus-stack-prometheus",
            },
        )
        self.assertEqual(
            authz["policy-reporter-plugins"]["spec"]["rules"][0]["from"][0]["source"][
                "notPrincipals"
            ],
            ["cluster.local/ns/kyverno/sa/policy-reporter-ui"],
        )

        netpols = {
            d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"
        }
        self.assertEqual(
            set(netpols),
            {
                "policy-reporter-ui",
                "policy-reporter-backends",
                "policy-reporter-oidc-proxy",
                "policy-reporter-openbao-clients",
                "allow-policy-reporter-openbao-clients",
            },
        )
