"""Scoped resource defaults: source contracts and owned real-API tests."""

import copy
import json
import time
import unittest
from pathlib import Path
from typing import Any

import yaml
import test_timezone_defaults as timezone

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "01-bootstrap/resource-defaults"
POLICY = COMPONENT / "resources/defaults.yaml"


def documents() -> list[dict[str, Any]]:
    """Load only the public policy definitions."""
    return list(yaml.safe_load_all(POLICY.read_text()))


def fixture(family: str, node: str = "omv") -> dict[str, Any]:
    """Build a tokenless fixture; this test API has no nodes or image execution."""
    longhorn = family == "longhorn"
    name = "instance-manager-probe" if longhorn else "svclb-probe"
    resource: dict[str, Any] = timezone.pod()
    resource["metadata"] = {
        "name": name,
        "namespace": "longhorn" if longhorn else "kube-system",
        "labels": (
            {
                "longhorn.io/component": "instance-manager",
                "longhorn.io/data-engine": "v1",
            }
            if longhorn
            else {
                "svccontroller.k3s.cattle.io/svcname": "synthetic-gateway",
                "svccontroller.k3s.cattle.io/svcnamespace": "synthetic-gateway",
            }
        ),
        "ownerReferences": [
            {
                "apiVersion": "longhorn.io/v1beta2" if longhorn else "apps/v1",
                "kind": "InstanceManager" if longhorn else "DaemonSet",
                "name": name,
                "uid": "00000000-0000-4000-8000-000000000001",
                "controller": True,
            }
        ],
    }
    resource["spec"]["serviceAccountName"] = (
        "longhorn-service-account" if longhorn else "svclb"
    )
    resource["spec"]["nodeName"] = node
    resource["spec"]["containers"] = [
        {
            "name": "instance-manager" if longhorn else "lb-tcp-8080",
            "image": "registry.k8s.io/pause:3.10",
            "args": ["synthetic-unchanged"],
            "resources": {"requests": {"cpu": "480m"}} if longhorn else {},
        }
    ]
    return resource


class ResourceAdmissionContractTest(unittest.TestCase):
    def test_only_two_namespaced_create_policies_and_their_bindings(self) -> None:
        self.assertTrue(
            POLICY.is_file(), "Scoped resource defaults are not implemented"
        )
        docs = documents()
        self.assertEqual(len(docs), 4)
        policies = [d for d in docs if d["kind"] == "MutatingAdmissionPolicy"]
        bindings = [d for d in docs if d["kind"] == "MutatingAdmissionPolicyBinding"]
        self.assertEqual(len(policies), 2)
        self.assertEqual(
            {b["spec"]["policyName"] for b in bindings},
            {p["metadata"]["name"] for p in policies},
        )
        for policy in policies:
            self.assertEqual(policy["apiVersion"], "admissionregistration.k8s.io/v1")
            spec = policy["spec"]
            self.assertEqual(spec["failurePolicy"], "Fail")
            self.assertEqual(spec["reinvocationPolicy"], "IfNeeded")
            self.assertEqual(
                spec["matchConstraints"]["resourceRules"],
                [
                    {
                        "apiGroups": [""],
                        "apiVersions": ["v1"],
                        "operations": ["CREATE"],
                        "resources": ["pods"],
                    }
                ],
            )
            namespace = spec["matchConstraints"]["namespaceSelector"]["matchLabels"][
                "kubernetes.io/metadata.name"
            ]
            self.assertIn(namespace, {"longhorn", "kube-system"})
            self.assertTrue(
                spec["matchConstraints"]["objectSelector"].get("matchLabels")
                or spec["matchConstraints"]["objectSelector"].get("matchExpressions")
            )
            self.assertTrue(spec["matchConditions"])

    def test_application_is_registered_and_does_not_own_workload_pods(self) -> None:
        self.assertTrue((COMPONENT / "app.yml").is_file())
        app = yaml.safe_load((COMPONENT / "app.yml").read_text())
        self.assertEqual(app["metadata"]["name"], "resource-defaults")
        self.assertEqual(
            app["spec"]["sources"],
            [
                {
                    "repoURL": "https://github.com/dmuiX/k3s-on-omv-infra.git",
                    "targetRevision": "main",
                    "path": "01-bootstrap/resource-defaults/resources",
                }
            ],
        )
        root = yaml.safe_load((ROOT / "kustomization.yaml").read_text())
        self.assertEqual(
            root["resources"].count("01-bootstrap/resource-defaults/app.yml"), 1
        )
        self.assertEqual(
            {d["kind"] for d in documents()},
            {"MutatingAdmissionPolicy", "MutatingAdmissionPolicyBinding"},
        )


@unittest.skipUnless(
    timezone.TEST_CONTAINER, "Owned isolated admission test API required"
)
class NativeResourceAdmissionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Reuse the existing exact-container/no-bind/no-network write guard and
        # activate timezone too: these two native admission features must compose.
        timezone.NativeTimezoneAdmissionTest.setUpClass()
        for namespace in ("longhorn", "kube-system", "resource-admission-other"):
            if namespace != "kube-system":
                timezone.NativeTimezoneAdmissionTest.kubectl(
                    "create", "namespace", namespace
                )
            for name in ("longhorn-service-account", "svclb", "unrelated"):
                timezone.NativeTimezoneAdmissionTest.kubectl(
                    "create",
                    "-f",
                    "-",
                    resource={
                        "apiVersion": "v1",
                        "kind": "ServiceAccount",
                        "metadata": {"name": name, "namespace": namespace},
                        "automountServiceAccountToken": False,
                    },
                )
        timezone.NativeTimezoneAdmissionTest.kubectl(
            "apply", "-f", "-", text=POLICY.read_text()
        )
        cls.wait_active()

    @classmethod
    def wait_active(cls) -> None:
        """Wait for BOTH policy bindings, not a sleep or one-policy witness."""
        deadline = time.monotonic() + 12
        while True:
            witness = cls.admit(fixture("longhorn"))
            loadbalancer = cls.admit(fixture("servicelb"))
            if witness["spec"]["containers"][0]["resources"].get("requests", {}).get(
                "memory"
            ) == "640Mi" and loadbalancer["spec"]["containers"][0]["resources"].get(
                "requests"
            ) == {
                "cpu": "5m",
                "memory": "8Mi",
            }:
                break
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "Resource defaults did not activate in the owned API"
                )
            time.sleep(0.2)

    @staticmethod
    def admit(resource: dict[str, Any]) -> dict[str, Any]:
        """Exercise server admission only inside the guarded disposable API."""
        result: dict[str, Any] = timezone.NativeTimezoneAdmissionTest.admit(resource)
        return result

    def test_longhorn_ram_is_node_sized_and_cpu_and_other_fields_survive(self) -> None:
        for node, memory in (
            ("raspi4", "448Mi"),
            ("omv", "640Mi"),
            ("wyse5070", "640Mi"),
        ):
            with self.subTest(node=node):
                resource = fixture("longhorn", node)
                original = resource["spec"]["containers"][0]
                original["resources"]["limits"] = {"cpu": "960m"}
                original["resources"]["requests"]["ephemeral-storage"] = "5Mi"
                actual = self.admit(resource)["spec"]["containers"][0]
                expected = copy.deepcopy(original["resources"])
                expected["requests"]["memory"] = memory
                self.assertEqual(actual["resources"], expected)
                self.assertEqual(actual["args"], original["args"])
                self.assertEqual(actual["image"], original["image"])
                self.assertEqual(timezone.tz_entries(actual), timezone.BERLIN_ENV)

    def test_servicelb_defaults_only_matching_containers(self) -> None:
        resource = fixture("servicelb")
        resource["spec"]["containers"] += [
            {"name": name, "image": "registry.k8s.io/pause:3.10"}
            for name in ("lb-tcp-9443", "unrelated")
        ]
        actual = self.admit(resource)["spec"]["containers"]
        for c in actual[:2]:
            self.assertEqual(
                c["resources"], {"requests": {"cpu": "5m", "memory": "8Mi"}}
            )
        self.assertEqual(actual[2].get("resources", {}), {})

    def test_explicit_requests_zero_and_api_limit_defaults_are_preserved(self) -> None:
        for family in ("longhorn", "servicelb"):
            for resources in (
                {"requests": {"cpu": "20m", "memory": "32Mi"}},
                {"requests": {"cpu": "0", "memory": "0"}},
                {"limits": {"cpu": "20m", "memory": "32Mi"}},
            ):
                with self.subTest(family=family, resources=resources):
                    resource = fixture(family)
                    resource["spec"]["containers"][0]["resources"] = resources
                    actual = self.admit(resource)["spec"]["containers"][0]["resources"]
                    expected = copy.deepcopy(resources)
                    if "limits" in expected:
                        expected["requests"] = copy.deepcopy(expected["limits"])
                    self.assertEqual(actual, expected)

    def test_partial_servicelb_requests_are_extended_not_replaced(self) -> None:
        for requests in (
            {"cpu": "20m"},
            {"memory": "32Mi"},
            {"ephemeral-storage": "5Mi"},
        ):
            resource = fixture("servicelb")
            resource["spec"]["containers"][0]["resources"] = {"requests": requests}
            expected = {"cpu": "5m", "memory": "8Mi", **requests}
            self.assertEqual(
                self.admit(resource)["spec"]["containers"][0]["resources"]["requests"],
                expected,
            )

    def test_wrong_namespace_account_owner_labels_and_engine_are_untouched(
        self,
    ) -> None:
        for family in ("longhorn", "servicelb"):
            cases = (
                "namespace",
                "account",
                "owner",
                "owner-kind",
                "owner-api",
                "owner-name",
                "controller",
                "labels",
                "container",
            ) + (("engine", "node") if family == "longhorn" else ("empty-label",))
            for case in cases:
                with self.subTest(family=family, case=case):
                    resource = fixture(family)
                    if case == "namespace":
                        resource["metadata"]["namespace"] = "resource-admission-other"
                    elif case == "account":
                        resource["spec"]["serviceAccountName"] = "unrelated"
                    elif case == "owner":
                        resource["metadata"].pop("ownerReferences")
                    elif case == "owner-kind":
                        resource["metadata"]["ownerReferences"][0][
                            "kind"
                        ] = "Deployment"
                    elif case == "owner-api":
                        resource["metadata"]["ownerReferences"][0][
                            "apiVersion"
                        ] = "batch/v1"
                    elif case == "owner-name":
                        resource["metadata"]["ownerReferences"][0]["name"] = "unrelated"
                    elif case == "node":
                        resource["spec"]["nodeName"] = "unmeasured-node"
                    elif case == "empty-label":
                        resource["metadata"]["labels"][
                            "svccontroller.k3s.cattle.io/svcname"
                        ] = ""
                    elif case == "controller":
                        resource["metadata"]["ownerReferences"][0]["controller"] = False
                    elif case == "labels":
                        resource["metadata"]["labels"] = {}
                    elif case == "engine":
                        resource["metadata"]["labels"]["longhorn.io/data-engine"] = "v2"
                    else:
                        resource["spec"]["containers"][0]["name"] = "unrelated"
                    expected = resource["spec"]["containers"][0]["resources"]
                    self.assertEqual(
                        self.admit(resource)["spec"]["containers"][0].get(
                            "resources", {}
                        ),
                        expected,
                    )

    def test_reinvocation_does_not_change_existing_values(self) -> None:
        for family in ("longhorn", "servicelb"):
            resource = fixture(family)
            first = self.admit(resource)
            resource["spec"] = first["spec"]
            self.assertEqual(self.admit(resource)["spec"], first["spec"])

    def test_update_does_not_retroactively_default_a_matching_pod(self) -> None:
        resource = fixture("servicelb")
        # Keep the persisted UPDATE witness separate from dry-run fixture names.
        resource["metadata"]["name"] = "svclb-update-probe"
        resource["spec"]["terminationGracePeriodSeconds"] = 0
        owners = resource["metadata"].pop("ownerReferences")
        created = timezone.NativeTimezoneAdmissionTest.admit(resource, dry_run=False)
        try:
            actual = yaml.safe_load(
                timezone.NativeTimezoneAdmissionTest.kubectl(
                    "patch",
                    "pod",
                    created["metadata"]["name"],
                    "-n",
                    "kube-system",
                    "--type=merge",
                    "-p",
                    json.dumps({"metadata": {"ownerReferences": owners}}),
                    "-o",
                    "json",
                )
            )
            self.assertEqual(actual["spec"]["containers"][0].get("resources", {}), {})
        finally:
            timezone.NativeTimezoneAdmissionTest.kubectl(
                "delete",
                "pod",
                created["metadata"]["name"],
                "-n",
                "kube-system",
                "--wait=true",
                "--timeout=5s",
                "--ignore-not-found",
            )
            self.assertEqual(
                timezone.NativeTimezoneAdmissionTest.kubectl(
                    "get",
                    "pod",
                    created["metadata"]["name"],
                    "-n",
                    "kube-system",
                    "--ignore-not-found",
                    "-o",
                    "name",
                ).strip(),
                "",
            )

    def test_without_bindings_native_api_does_not_invent_the_defaults(self) -> None:
        bindings = [
            d for d in documents() if d["kind"] == "MutatingAdmissionPolicyBinding"
        ]
        try:
            for binding in bindings:
                timezone.NativeTimezoneAdmissionTest.kubectl(
                    "delete",
                    "mutatingadmissionpolicybinding",
                    binding["metadata"]["name"],
                )
            deadline = time.monotonic() + 12
            while True:
                lh = self.admit(fixture("longhorn"))["spec"]["containers"][0][
                    "resources"
                ]
                lb = self.admit(fixture("servicelb"))["spec"]["containers"][0].get(
                    "resources", {}
                )
                if "memory" not in lh.get("requests", {}) and not lb:
                    break
                if time.monotonic() >= deadline:
                    self.fail("Deleted bindings still default requests")
                time.sleep(0.2)
        finally:
            timezone.NativeTimezoneAdmissionTest.kubectl(
                "apply", "-f", "-", text=yaml.safe_dump_all(bindings)
            )
            self.wait_active()

    def test_init_containers_are_not_accidentally_defaulted(self) -> None:
        for family in ("longhorn", "servicelb"):
            resource = fixture(family)
            init_name = resource["spec"]["containers"][0]["name"]
            resource["spec"]["containers"][0]["name"] = "unrelated"
            resource["spec"]["initContainers"] = [
                {
                    "name": init_name,
                    "image": "registry.k8s.io/pause:3.10",
                }
            ]
            actual = self.admit(resource)["spec"]["initContainers"][0]
            self.assertEqual(actual.get("resources", {}), {})


if __name__ == "__main__":
    unittest.main()
