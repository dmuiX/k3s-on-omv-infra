"""Offline contracts for the Tekton UI, OIDC boundary and credential delivery."""

import subprocess
import unittest
from typing import ClassVar
from urllib.parse import urlsplit

import yaml

from workload_contracts import Document, Repository

APP = "tekton-dashboard"
PROXY = "tekton-dashboard-oidc-proxy"
NAMESPACE = "tekton-pipelines"


class TektonDashboardTest(unittest.TestCase):
    repo: ClassVar[Repository]
    docs: ClassVar[dict[tuple[str, str], Document]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Repository()
        cls.docs = {
            (doc["kind"], doc["metadata"]["name"]): doc for doc in cls.repo.render(APP)
        }

    def spec(self, kind: str, name: str) -> Document:
        spec: Document = self.docs[kind, name]["spec"]
        return spec

    def test_route_targets_only_oidc_proxy_and_preserves_its_identity(self) -> None:
        route = self.spec("HTTPRoute", APP)
        self.assertEqual(
            route["parentRefs"],
            [
                {
                    "group": "gateway.networking.k8s.io",
                    "kind": "Gateway",
                    "name": "istio-gateway",
                    "namespace": "istio-gateway",
                    "sectionName": "https",
                }
            ],
        )
        (rule,) = route["rules"]
        self.assertEqual(
            rule["backendRefs"],
            [{"group": "", "kind": "Service", "name": PROXY, "port": 80, "weight": 1}],
        )
        self.assertEqual(
            rule["matches"], [{"path": {"type": "PathPrefix", "value": "/"}}]
        )
        (headers,) = rule["filters"]
        self.assertEqual(
            set(headers["requestHeaderModifier"]["remove"]),
            {
                "Authorization",
                "Impersonate-User",
                "Impersonate-Group",
                "Impersonate-Uid",
                "Sec-WebSocket-Protocol",
            },
        )
        self.assertEqual(rule["timeouts"], {"request": "0s"})
        policy = self.spec("NetworkPolicy", APP)
        self.assertEqual(
            policy["ingress"],
            [
                {
                    "from": [
                        {
                            "podSelector": {
                                "matchLabels": {"app.kubernetes.io/name": PROXY}
                            }
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 9097}],
                }
            ],
        )
        self.assertEqual(policy["egress"], [])
        api_source = next(
            s for s in self.repo.apps[APP]["spec"]["sources"] if "ref" in s
        )
        self.assertEqual(api_source["path"], "infra/tekton-dashboard")
        self.assertEqual(api_source["directory"], {"include": "network-peers.yaml"})

    def test_dashboard_can_only_read_ci_builds_and_named_version_maps(self) -> None:
        pod = self.spec("Deployment", APP)["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], APP)
        (container,) = pod["containers"]
        self.assertIn("--read-only=true", container["args"])
        self.assertIn("--namespaces=ci-builds", container["args"])
        self.assertIn("--logout-url=/oauth2/sign_out", container["args"])
        self.assertRegex(container["image"], r"v0\.74\.0@sha256:[a-f0-9]{64}$")
        self.assertTrue(container["readinessProbe"] and container["livenessProbe"])
        self.assertFalse(
            any(kind in {"ClusterRole", "ClusterRoleBinding"} for kind, _ in self.docs)
        )
        for name, namespace in (
            ("tekton-dashboard-info", NAMESPACE),
            ("tekton-dashboard-reader", "ci-builds"),
        ):
            role = self.docs["Role", name]
            binding = self.docs["RoleBinding", name]
            self.assertEqual(role["metadata"]["namespace"], namespace)
            self.assertEqual(binding["metadata"]["namespace"], namespace)
            self.assertEqual(binding["roleRef"]["name"], name)
            self.assertEqual(
                binding["subjects"],
                [{"kind": "ServiceAccount", "name": APP, "namespace": NAMESPACE}],
            )
            for rule in role["rules"]:
                self.assertLessEqual(set(rule["verbs"]), {"get", "list", "watch"})
                self.assertFalse(
                    set(rule["resources"])
                    & {"*", "secrets", "pods/exec", "pods/portforward"}
                )
                if namespace == NAMESPACE:
                    self.assertEqual(rule["resources"], ["configmaps"])
                    self.assertEqual(
                        set(rule["resourceNames"]),
                        {"dashboard-info", "pipelines-info", "triggers-info"},
                    )
        resources = {
            r
            for rule in self.docs["Role", "tekton-dashboard-reader"]["rules"]
            for r in rule["resources"]
        }
        self.assertTrue(
            {"pipelines", "pipelineruns", "tasks", "taskruns", "pods/log"} <= resources
        )

    def test_oidc_client_callback_and_session_gate_agree(self) -> None:
        config = self.docs["ConfigMap", PROXY + "-inputs"]["data"]
        proxy = self.spec("Deployment", PROXY)["template"]
        self.assertEqual(
            proxy["metadata"]["labels"]["istio.io/dataplane-mode"], "ambient"
        )
        self.assertEqual(
            proxy["spec"]["securityContext"]["seccompProfile"]["type"], "RuntimeDefault"
        )
        (container,) = proxy["spec"]["containers"]
        args = set(container["args"])
        internal = f"http://openbao-active.openbao.svc.cluster.local:8200/v1/identity/oidc/provider/{APP}"
        public_login = config["issuer-url"].replace(
            "/v1/identity/", "/ui/vault/identity/", 1
        )
        self.assertTrue(
            {
                "--provider=oidc",
                "--pass-basic-auth=false",
                "--pass-authorization-header=false",
                "--skip-oidc-discovery=true",
                "--skip-claims-from-profile-url=true",
                f"--login-url={public_login}/authorize",
                f"--redeem-url={internal}/token",
                f"--oidc-jwks-url={internal}/.well-known/keys",
                "--allowed-group=radar:admin",
                f"--scope=openid {APP}",
                f"--upstream=http://{APP}.{NAMESPACE}.svc:9097",
            }
            <= args
        )
        client = self.spec("OidcClient", APP)["forProvider"]
        provider = self.spec("OidcProvider", APP)["forProvider"]
        self.assertEqual(client["clientType"], "confidential")
        self.assertEqual(client["assignments"], ["tekton-dashboard-admin"])
        self.assertEqual(client["redirectUris"], [config["redirect-url"]])
        redirect = urlsplit(config["redirect-url"])
        self.assertEqual(
            (redirect.scheme, redirect.path), ("https", "/oauth2/callback")
        )
        self.assertIn(redirect.hostname, self.spec("HTTPRoute", APP)["hostnames"])
        self.assertEqual(provider["issuerHost"], urlsplit(config["issuer-url"]).netloc)
        self.assertEqual(provider["allowedClientIdsRefs"], [{"name": APP}])
        self.assertEqual(provider["scopesSupportedRefs"], [{"name": APP}])
        assignment = self.spec("OidcAssignment", "tekton-dashboard-admin")[
            "forProvider"
        ]
        self.assertEqual(assignment["groupIdsRefs"], [{"name": "radar-admin"}])
        self.assertEqual(proxy["spec"]["serviceAccountName"], PROXY)
        self.assertIs(
            self.docs["ServiceAccount", PROXY]["automountServiceAccountToken"], False
        )
        destination = self.spec("VaultStaticSecret", PROXY)["destination"]
        self.assertEqual(destination["name"], PROXY)
        self.assertEqual(
            set(destination["transformation"]["templates"]),
            {"client-id", "client-secret", "cookie-secret"},
        )
        refs = [
            v["valueFrom"]["secretKeyRef"]
            for v in container["env"]
            if "secretKeyRef" in v["valueFrom"]
        ]
        self.assertEqual({ref["name"] for ref in refs}, {PROXY})
        self.assertEqual(
            {ref["key"] for ref in refs},
            set(destination["transformation"]["templates"]),
        )

    def test_backchannel_rejects_different_provider(self) -> None:
        source = next(
            s
            for s in self.repo.apps[APP]["spec"]["sources"]
            if s.get("path") == "charts/oidc-proxy"
        )
        values = self.repo.values(source)
        values["openbaoBackchannelURL"] = (
            values["openbaoBackchannelURL"].removesuffix(APP) + "longhorn"
        )
        result = subprocess.run(
            [
                "helm",
                "template",
                PROXY,
                str(self.repo.root / source["path"]),
                "-f",
                "-",
            ],
            input=yaml.safe_dump(values),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("same OpenBao OIDC provider", result.stderr)
