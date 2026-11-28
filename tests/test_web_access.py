"""Offline contract and equivalence tests for charts/web-access."""

from __future__ import annotations

from collections.abc import Callable
import copy
from pathlib import Path
import subprocess
from typing import Any
import unittest

import yaml

INFRA = Path(__file__).resolve().parents[1]
CHART = INFRA / "charts/web-access"
FIXTURE = INFRA / "tests/fixtures/web-access/full-stack-values.yaml"
CUSTODY_ADAPTER = INFRA / "adapters/credential-custody/resources.yaml"
ISSUER_BASE = "https://openbao.example.invalid:9443/v1/identity/oidc/provider"
BACKCHANNEL_BASE = (
    "http://openbao-active.openbao.svc.cluster.local:8200/v1/identity/oidc/provider"
)


def render_chart(values: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "irrelevant-release", str(CHART), "-f", "-"],
        input=yaml.safe_dump(values),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def render_docs(values: dict[str, Any]) -> list[dict[str, Any]]:
    result = render_chart(values)
    if result.returncode != 0:
        raise AssertionError(
            f"helm template failed ({result.returncode}): {result.stderr}"
        )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def by_kind(docs: list[dict[str, Any]], kind_name: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc.get("kind") == kind_name]


def base_full_stack() -> dict[str, Any]:
    loaded = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return dict(loaded)


class WebAccessChartTests(unittest.TestCase):
    def test_helm_lint_strict_passes_with_fixture(self) -> None:
        result = subprocess.run(
            ["helm", "lint", str(CHART), "--strict", "-f", str(FIXTURE)],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_route_only_profile_matches_openbao_shape(self) -> None:
        values: dict[str, Any] = {
            "name": "openbao",
            "namespace": "openbao",
            "route": {
                "hostnames": ["openbao.example.invalid"],
                "backend": {"name": "openbao-active", "port": 8200},
            },
        }
        docs = render_docs(values)
        self.assertEqual([doc["kind"] for doc in docs], ["HTTPRoute"])
        route = docs[0]
        self.assertEqual(route["metadata"]["name"], "openbao")
        self.assertEqual(route["metadata"]["namespace"], "openbao")
        self.assertNotIn("annotations", route["metadata"])
        self.assertEqual(
            route["spec"]["parentRefs"],
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
        self.assertEqual(
            route["spec"]["rules"],
            [
                {
                    "backendRefs": [
                        {
                            "group": "",
                            "kind": "Service",
                            "name": "openbao-active",
                            "port": 8200,
                            "weight": 1,
                        }
                    ],
                    "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                }
            ],
        )

    def test_full_stack_derives_conventions_and_shares_oidc_urls(self) -> None:
        docs = render_docs(base_full_stack())
        kinds = [doc["kind"] for doc in docs]
        self.assertCountEqual(
            kinds,
            [
                "HTTPRoute",
                "OidcAssignment",
                "OidcKey",
                "OidcScope",
                "OidcClient",
                "OidcProvider",
                "ConfigMap",
                "ConfigMap",
                "Deployment",
                "Service",
                "ServiceAccount",
                "ConfigMap",
                "Job",
            ],
        )

        (route,) = by_kind(docs, "HTTPRoute")
        self.assertEqual(
            route["spec"]["rules"][0]["backendRefs"],
            [
                {
                    "group": "",
                    "kind": "Service",
                    "name": "example-app-oidc-proxy",
                    "port": 80,
                    "weight": 1,
                }
            ],
        )

        (deployment,) = by_kind(docs, "Deployment")
        self.assertEqual(deployment["metadata"]["name"], "example-app-oidc-proxy")
        self.assertEqual(
            deployment["metadata"]["annotations"],
            {"argocd.argoproj.io/sync-wave": "20"},
        )
        pod_spec = deployment["spec"]["template"]["spec"]
        self.assertEqual(
            pod_spec["securityContext"],
            {"seccompProfile": {"type": "RuntimeDefault"}},
        )
        args = pod_spec["containers"][0]["args"]
        self.assertIn("--scope=openid example-app", args)
        self.assertIn("--allowed-group=platform:admin", args)
        self.assertIn("--pass-basic-auth=false", args)
        self.assertIn("--pass-authorization-header=false", args)
        self.assertIn(f"--redeem-url={BACKCHANNEL_BASE}/example-app/token", args)

        configmaps = {cm["metadata"]["name"]: cm for cm in by_kind(docs, "ConfigMap")}
        inputs_cm = configmaps["example-app-oidc-proxy-inputs"]
        self.assertEqual(
            inputs_cm["data"],
            {
                "issuer-url": f"{ISSUER_BASE}/example-app",
                "redirect-url": "https://example-app.example.invalid:9443/oauth2/callback",
            },
        )

        (job,) = by_kind(docs, "Job")
        self.assertEqual(job["metadata"]["name"], "example-app-oidc-provisioner")
        self.assertEqual(
            job["spec"]["template"]["spec"]["serviceAccountName"],
            "example-app-oidc-provisioner",
        )

    def test_credential_custody_matches_adapter_byte_for_byte(self) -> None:
        docs = render_docs(base_full_stack())
        configmaps = {cm["metadata"]["name"]: cm for cm in by_kind(docs, "ConfigMap")}
        rendered = configmaps["credential-custody"]
        adapter = yaml.safe_load(CUSTODY_ADAPTER.read_text(encoding="utf-8"))
        self.assertEqual(rendered["metadata"]["name"], adapter["metadata"]["name"])
        self.assertEqual(
            rendered["metadata"]["annotations"], adapter["metadata"]["annotations"]
        )
        self.assertEqual(rendered["metadata"]["namespace"], "example-ns")
        self.assertEqual(rendered["data"], adapter["data"])

    def test_oidc_credential_helper_can_be_disabled(self) -> None:
        values = base_full_stack()
        values["oidc"]["credentialHelper"] = False
        values["proxy"]["enabled"] = False
        values["route"]["enabled"] = False
        docs = render_docs(values)
        self.assertEqual(by_kind(docs, "ConfigMap"), [])

    def test_oidc_only_radar_and_audiobookshelf_profiles(self) -> None:
        radar_values: dict[str, Any] = {
            "name": "radar",
            "namespace": "radar",
            "route": {"enabled": False},
            "oidc": {
                "enabled": True,
                "issuerURL": f"{ISSUER_BASE}/radar",
                "redirectURLs": ["https://radar.example.invalid:9443/auth/callback"],
                "groups": [
                    {
                        "name": "platform-admin",
                        "syncWave": "-20",
                        "externalName": "platform:admin",
                    }
                ],
                "assignments": [],
                "key": {"syncWave": "-20"},
                "scopes": [{"name": "radar", "syncWave": "-20"}],
                "client": {
                    "syncWave": "-20",
                    "assignments": ["allow_all"],
                    "connectionSecret": {
                        "name": "radar-oidc-client",
                        "namespace": "radar",
                    },
                },
                "provider": {"syncWave": "-20"},
            },
        }
        radar_docs = render_docs(radar_values)
        self.assertEqual(by_kind(radar_docs, "HTTPRoute"), [])
        self.assertEqual(by_kind(radar_docs, "OidcAssignment"), [])
        (group,) = by_kind(radar_docs, "Group")
        self.assertEqual(group["spec"]["forProvider"]["name"], "platform:admin")
        (client,) = by_kind(radar_docs, "OidcClient")
        self.assertEqual(client["spec"]["forProvider"]["assignments"], ["allow_all"])
        self.assertEqual(
            client["spec"]["writeConnectionSecretToRef"],
            {"name": "radar-oidc-client", "namespace": "radar"},
        )
        self.assertEqual(len(by_kind(radar_docs, "ConfigMap")), 1)

        abs_values: dict[str, Any] = {
            "name": "audiobookshelf",
            "namespace": "audiobookshelf",
            "route": {"enabled": False},
            "oidc": {
                "enabled": True,
                "issuerURL": f"{ISSUER_BASE}/audiobookshelf",
                "redirectURLs": [
                    "https://abs.example.invalid:9443/auth/openid/callback",
                    "https://abs.example.invalid:9443/auth/openid/mobile-redirect",
                ],
                "assignments": [{"name": "audiobookshelf-users", "syncWave": "-30"}],
                "key": {"syncWave": "-30"},
                "scopes": [
                    {
                        "name": "audiobookshelf",
                        "syncWave": "-30",
                        "template": '{"audiobookshelf":["admin"]}',
                    }
                ],
                "client": {
                    "syncWave": "-20",
                    "connectionSecret": {
                        "name": "audiobookshelf-oidc-connection",
                        "namespace": "audiobookshelf",
                    },
                },
                "provider": {
                    "syncWave": "-15",
                    "scopeRefs": ["oidc-profile", "audiobookshelf"],
                },
            },
        }
        abs_docs = render_docs(abs_values)
        (abs_provider,) = by_kind(abs_docs, "OidcProvider")
        self.assertEqual(
            abs_provider["spec"]["forProvider"]["scopesSupportedRefs"],
            [{"name": "oidc-profile"}, {"name": "audiobookshelf"}],
        )
        (abs_client,) = by_kind(abs_docs, "OidcClient")
        self.assertEqual(
            abs_client["spec"]["forProvider"]["assignments"],
            ["audiobookshelf-users"],
        )

    def test_route_sync_wave_timeouts_and_legacy_pass_basic_auth(self) -> None:
        values = base_full_stack()
        values["route"]["syncWave"] = "21"
        values["route"]["timeouts"] = {"request": "0s"}
        values["proxy"]["passBasicAuth"] = True
        values["proxy"]["provisioner"]["name"] = "example-app-provisioner"
        docs = render_docs(values)
        (route,) = by_kind(docs, "HTTPRoute")
        self.assertEqual(
            route["metadata"]["annotations"],
            {"argocd.argoproj.io/sync-wave": "21"},
        )
        self.assertEqual(route["spec"]["rules"][0]["timeouts"], {"request": "0s"})
        (deployment,) = by_kind(docs, "Deployment")
        args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertNotIn("--pass-basic-auth=false", args)
        (job,) = by_kind(docs, "Job")
        self.assertEqual(job["metadata"]["name"], "example-app-provisioner")

    def test_invalid_inputs_fail_closed(self) -> None:
        base = base_full_stack()
        cases: list[tuple[str, Callable[[dict[str, Any]], object]]] = [
            ("unknown-root-key", lambda v: v.update(extraKey=True)),
            ("empty-name", lambda v: v.update(name="")),
            ("empty-namespace", lambda v: v.update(namespace="")),
            ("missing-hostnames", lambda v: v["route"].update(hostnames=[])),
            (
                "route-without-backend-or-proxy",
                lambda v: (
                    v["proxy"].update(enabled=False),
                    v["route"].update(backend={"name": "", "port": 80, "weight": 1}),
                ),
            ),
            (
                "http-issuer-url",
                lambda v: v["oidc"].update(
                    issuerURL="http://openbao.example.invalid/v1/identity/oidc/provider/example-app"
                ),
            ),
            (
                "wrong-issuer-path",
                lambda v: v["oidc"].update(issuerURL=f"{ISSUER_BASE}/other"),
            ),
            (
                "empty-redirect-urls",
                lambda v: v["oidc"].update(redirectURLs=[]),
            ),
            (
                "empty-assignments-without-client-assignments",
                lambda v: v["oidc"].update(assignments=[]),
            ),
            (
                "http-backchannel-without-ambient",
                lambda v: v["proxy"].update(
                    ambient=False,
                    openbaoBackchannelURL=f"{BACKCHANNEL_BASE}/example-app",
                ),
            ),
            (
                "invalid-timeout-format",
                lambda v: v["route"].update(timeouts={"request": "forever"}),
            ),
            (
                "invalid-filter-type",
                lambda v: v["route"].update(filters=[{"type": "ExtensionRef"}]),
            ),
        ]
        for label, mutate in cases:
            with self.subTest(case=label):
                candidate = copy.deepcopy(base)
                mutate(candidate)
                result = render_chart(candidate)
                self.assertNotEqual(
                    result.returncode,
                    0,
                    f"Expected failure for {label}, got success",
                )


if __name__ == "__main__":
    unittest.main()
