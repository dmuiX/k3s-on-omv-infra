"""Offline contracts and opt-in enforcement tests against an isolated K3s API."""

from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "01-bootstrap/timezone-defaults"
POLICY_FILE = COMPONENT / "resources/defaults.yaml"
DOCUMENTS = list(yaml.safe_load_all(POLICY_FILE.read_text()))
TEST_CONTAINER = os.environ.get("TIMEZONE_TEST_CONTAINER", "")
BERLIN_ENV = [{"name": "TZ", "value": "Europe/Berlin"}]


def pod(containers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build an unscheduled, tokenless Pod fixture; no image is executed."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"generateName": "timezone-admission-probe-"},
        "spec": {
            "automountServiceAccountToken": False,
            "serviceAccountName": "timezone-admission-test",
            "restartPolicy": "Never",
            "containers": containers
            or [{"name": "main", "image": "registry.k8s.io/pause:3.10"}],
        },
    }


def cronjob() -> dict[str, Any]:
    """Build a suspended CronJob fixture that cannot start a workload."""
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"generateName": "timezone-admission-probe-"},
        "spec": {
            "schedule": "35 18 * * *",
            "suspend": True,
            "jobTemplate": {"spec": {"template": {"spec": pod()["spec"]}}},
        },
    }


def tz_entries(container: dict[str, Any]) -> list[dict[str, Any]]:
    """Inspect only the fixture's TZ entries, never an actual environment."""
    return [entry for entry in container.get("env", []) if entry["name"] == "TZ"]


class TimezoneContractTest(unittest.TestCase):
    def test_native_mutation_and_deny_bindings_fail_closed(self) -> None:
        self.assertEqual(len(DOCUMENTS), 8)
        policies = {
            d["metadata"]["name"]: d for d in DOCUMENTS if d["kind"].endswith("Policy")
        }
        bindings = [d for d in DOCUMENTS if d["kind"].endswith("PolicyBinding")]
        self.assertEqual(len(policies), 4)
        self.assertEqual(len(bindings), 4)
        self.assertEqual({b["spec"]["policyName"] for b in bindings}, set(policies))
        for document in DOCUMENTS:
            self.assertEqual(document["apiVersion"], "admissionregistration.k8s.io/v1")
        for policy in policies.values():
            self.assertEqual(policy["spec"]["failurePolicy"], "Fail")
            if policy["kind"] == "MutatingAdmissionPolicy":
                self.assertEqual(policy["spec"]["reinvocationPolicy"], "IfNeeded")
        for binding in bindings:
            self.assertNotIn("matchResources", binding["spec"])
            if binding["kind"] == "ValidatingAdmissionPolicyBinding":
                self.assertEqual(binding["spec"]["validationActions"], ["Deny"])

    def test_namespace_global_admission_scope(self) -> None:
        for document in (d for d in DOCUMENTS if d["kind"].endswith("Policy")):
            rules = document["spec"]["matchConstraints"]
            self.assertEqual(set(rules), {"resourceRules"})
            if "cronjob" in document["metadata"]["name"]:
                expected = [
                    {
                        "apiGroups": ["batch"],
                        "apiVersions": ["v1"],
                        "operations": ["CREATE", "UPDATE"],
                        "resources": ["cronjobs"],
                    }
                ]
            else:
                expected = [
                    {
                        "apiGroups": [""],
                        "apiVersions": ["v1"],
                        "operations": ["CREATE"],
                        "resources": ["pods"],
                    },
                    {
                        "apiGroups": [""],
                        "apiVersions": ["v1"],
                        "operations": ["UPDATE"],
                        "resources": ["pods/ephemeralcontainers"],
                    },
                ]
            self.assertEqual(rules["resourceRules"], expected)

    def test_application_is_canonical_and_before_platform_controllers(self) -> None:
        app = yaml.safe_load((COMPONENT / "app.yml").read_text())
        self.assertEqual(
            app["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "1"
        )
        self.assertEqual(
            app["spec"]["sources"][0]["path"],
            "01-bootstrap/timezone-defaults/resources",
        )
        self.assertEqual(app["spec"]["sources"][0]["targetRevision"], "main")
        self.assertEqual(
            app["spec"]["syncPolicy"]["automated"],
            {"enabled": True, "prune": False, "selfHeal": True, "allowEmpty": False},
        )
        self.assertIn(
            "01-bootstrap/timezone-defaults/app.yml",
            yaml.safe_load((ROOT / "kustomization.yaml").read_text())["resources"],
        )

    def test_berlin_trim_wall_time_in_winter_and_summer(self) -> None:
        for month, utc_hour in ((1, 17), (7, 16)):
            local = datetime(2027, month, 16, 18, 35, tzinfo=ZoneInfo("Europe/Berlin"))
            self.assertEqual(
                local.astimezone(timezone.utc),
                datetime(2027, month, 16, utc_hour, 35, tzinfo=timezone.utc),
            )


@unittest.skipUnless(
    TEST_CONTAINER, "Set TIMEZONE_TEST_CONTAINER for isolated native admission tests"
)
class NativeTimezoneAdmissionTest(unittest.TestCase):
    @classmethod
    def kubectl(
        cls, *args: str, resource: dict[str, Any] | None = None, text: str | None = None
    ) -> str:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                TEST_CONTAINER,
                "/bin/kubectl",
                "--kubeconfig=/etc/rancher/k3s/k3s.yaml",
                *args,
            ],
            input=json.dumps(resource) if resource is not None else text,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout

    @classmethod
    def admit(
        cls, resource: dict[str, Any], operation: str = "create", dry_run: bool = True
    ) -> dict[str, Any]:
        args = [operation, "-f", "-", "-o", "json"]
        if dry_run:
            args.append("--dry-run=server")
        result: dict[str, Any] = json.loads(cls.kubectl(*args, resource=resource))
        return result

    @classmethod
    def ephemeral(
        cls, resource: dict[str, Any], dry_run: bool = True
    ) -> dict[str, Any]:
        patch = [
            {
                "op": "add",
                "path": "/spec/ephemeralContainers",
                "value": resource["spec"]["ephemeralContainers"],
            }
        ]
        args = [
            "patch",
            "pod",
            resource["metadata"]["name"],
            "--subresource=ephemeralcontainers",
            "--type=json",
            "-p",
            json.dumps(patch),
            "-o",
            "json",
        ]
        if dry_run:
            args.append("--dry-run=server")
        result: dict[str, Any] = json.loads(cls.kubectl(*args))
        return result

    @classmethod
    def require_isolated_api(cls) -> None:
        # Guard every API write: never use the host context or a mounted kubeconfig.
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "purpose"}}|{{.HostConfig.NetworkMode}}|'
                "{{.HostConfig.Privileged}}|{{.HostConfig.PidMode}}|{{.State.Status}}|"
                '{{range .Mounts}}{{if eq .Type "bind"}}bind{{end}}{{end}}',
                TEST_CONTAINER,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        if result.stdout.strip() != "timezone-policy-test|none|false||running|":
            raise AssertionError(
                "Native tests require the owned, network-isolated test container"
            )

    @classmethod
    def setUpClass(cls) -> None:
        cls.require_isolated_api()
        cls.kubectl("apply", "-f", "-", text=POLICY_FILE.read_text())
        cls.kubectl(
            "apply",
            "-f",
            "-",
            resource={
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "timezone-admission-test"},
                "automountServiceAccountToken": False,
            },
        )
        deadline = time.monotonic() + 10
        while True:
            witness = cls.admit(pod())
            if (
                tz_entries(witness["spec"]["containers"][0]) == BERLIN_ENV
                and cls.admit(cronjob())["spec"].get("timeZone") == "Europe/Berlin"
            ):
                break
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "Native admission did not activate timezone enforcement"
                )
            time.sleep(0.2)

    @contextmanager
    def without_mutators(self, include_validation: bool = False) -> Iterator[None]:
        bindings = [
            d
            for d in DOCUMENTS
            if d["kind"].endswith("PolicyBinding")
            and (include_validation or d["kind"] == "MutatingAdmissionPolicyBinding")
        ]
        try:
            for binding in bindings:
                self.kubectl(
                    "delete", binding["kind"].lower(), binding["metadata"]["name"]
                )
            yield
        finally:
            self.kubectl("apply", "-f", "-", text=yaml.safe_dump_all(bindings))
            deadline = time.monotonic() + 10
            while True:
                try:
                    witness = self.admit(pod())
                    self.assertEqual(
                        tz_entries(witness["spec"]["containers"][0]), BERLIN_ENV
                    )
                    self.assertEqual(
                        self.admit(cronjob())["spec"].get("timeZone"), "Europe/Berlin"
                    )
                    break
                except AssertionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)

    def assert_denied(self, resource: dict[str, Any], ephemeral: bool = False) -> None:
        deadline = time.monotonic() + 10
        while True:
            try:
                if ephemeral:
                    self.ephemeral(resource)
                else:
                    self.admit(resource)
            except AssertionError as error:
                self.assertIn("Europe/Berlin", str(error))
                break
            if time.monotonic() >= deadline:
                self.fail(
                    "Admission accepted a non-Berlin configuration without mutation"
                )
            time.sleep(0.2)

    def test_pod_mutation_preserves_other_fields_and_environment(self) -> None:
        container = {
            "name": "main",
            "image": "registry.k8s.io/pause:3.10",
            "args": ["original"],
            "env": [{"name": "KEEP", "value": "original"}],
        }
        actual = self.admit(pod([container]))["spec"]["containers"][0]
        self.assertEqual(tz_entries(actual), BERLIN_ENV)
        self.assertEqual(actual["args"], container["args"])
        self.assertEqual(actual["image"], container["image"])
        self.assertIn(container["env"][0], actual["env"])

    def test_literal_empty_and_valuefrom_timezone_are_forced_to_berlin(self) -> None:
        for entry in (
            {"name": "TZ", "value": "UTC"},
            {"name": "TZ", "value": ""},
            {
                "name": "TZ",
                "valueFrom": {"secretKeyRef": {"name": "tz-reference", "key": "TZ"}},
            },
            {
                "name": "TZ",
                "valueFrom": {"configMapKeyRef": {"name": "tz-reference", "key": "TZ"}},
            },
        ):
            with self.subTest(entry=entry):
                resource = pod()
                resource["spec"]["containers"][0]["env"] = [entry]
                self.assertEqual(
                    tz_entries(self.admit(resource)["spec"]["containers"][0]),
                    BERLIN_ENV,
                )

    def test_envfrom_gets_literal_override_without_reading_sources(self) -> None:
        for field in ("secretRef", "configMapRef"):
            resource = pod()
            resource["spec"]["containers"][0]["envFrom"] = [
                {field: {"name": "tz-reference"}}
            ]
            actual = self.admit(resource)["spec"]["containers"][0]
            self.assertEqual(tz_entries(actual), BERLIN_ENV)
            self.assertEqual(
                actual["envFrom"], resource["spec"]["containers"][0]["envFrom"]
            )

    def test_all_containers_and_init_sidecars_are_forced_without_reordering(
        self,
    ) -> None:
        resource = pod()
        resource["spec"]["initContainers"] = [
            {"name": "init", "image": "registry.k8s.io/pause:3.10"},
            {
                "name": "sidecar",
                "image": "registry.k8s.io/pause:3.10",
                "restartPolicy": "Always",
            },
            {
                "name": "referenced-init",
                "image": "registry.k8s.io/pause:3.10",
                "env": [
                    {
                        "name": "TZ",
                        "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                    }
                ],
            },
            {
                "name": "imported-init",
                "image": "registry.k8s.io/pause:3.10",
                "envFrom": [{"configMapRef": {"name": "tz-reference"}}],
            },
        ]
        resource["spec"]["containers"].append(
            {
                "name": "utc",
                "image": "registry.k8s.io/pause:3.10",
                "env": [{"name": "TZ", "value": "UTC"}],
            }
        )
        actual = self.admit(resource)["spec"]
        self.assertEqual(
            [c["name"] for c in actual["initContainers"]],
            [c["name"] for c in resource["spec"]["initContainers"]],
        )
        for container in actual["initContainers"] + actual["containers"]:
            self.assertEqual(tz_entries(container), BERLIN_ENV)
        self.assertEqual(actual["initContainers"][1]["restartPolicy"], "Always")

    def test_other_environment_references_and_order_are_preserved(self) -> None:
        resource = pod()
        entries: list[dict[str, Any]] = [
            {"name": "FIRST", "value": "original"},
            {
                "name": "REFERENCE",
                "valueFrom": {
                    "secretKeyRef": {"name": "opaque-reference", "key": "unused"}
                },
            },
            {
                "name": "TZ",
                "valueFrom": {"configMapKeyRef": {"name": "tz-reference", "key": "TZ"}},
            },
            {"name": "LAST", "value": "original"},
        ]
        resource["spec"]["containers"][0]["env"] = entries
        actual = self.admit(resource)["spec"]["containers"][0]
        self.assertEqual(tz_entries(actual), BERLIN_ENV)
        self.assertEqual(
            [e for e in actual["env"] if e["name"] != "TZ"],
            [e for e in entries if e["name"] != "TZ"],
        )

    def test_reapplication_is_idempotent(self) -> None:
        resource = pod()
        resource["spec"] = self.admit(resource)["spec"]
        self.assertEqual(
            tz_entries(self.admit(resource)["spec"]["containers"][0]), BERLIN_ENV
        )

    def test_windows_pod_also_declares_berlin(self) -> None:
        resource = pod()
        resource["spec"]["os"] = {"name": "windows"}
        self.assertEqual(
            tz_entries(self.admit(resource)["spec"]["containers"][0]), BERLIN_ENV
        )

    def test_all_cronjob_timezone_inputs_are_forced(self) -> None:
        for value in (None, "", "UTC", "America/New_York", "Europe/Berlin"):
            resource = cronjob()
            if value is not None:
                resource["spec"]["timeZone"] = value
            actual = self.admit(resource)["spec"]
            self.assertEqual(actual["timeZone"], "Europe/Berlin")
            self.assertEqual(actual["schedule"], resource["spec"]["schedule"])

    def test_cronjob_update_forces_even_an_explicit_timezone(self) -> None:
        resource = self.admit(cronjob(), dry_run=False)
        resource["spec"]["timeZone"] = "UTC"
        self.assertEqual(
            self.admit(resource, operation="replace")["spec"]["timeZone"],
            "Europe/Berlin",
        )

    def test_actual_git_owned_cronjobs_use_the_central_policy(self) -> None:
        found: set[tuple[str, str]] = set()
        for path in ROOT.glob("[0-9][0-9]-*/**/*.yaml"):
            for resource in yaml.safe_load_all(path.read_text()):
                if not isinstance(resource, dict) or resource.get("kind") != "CronJob":
                    continue
                namespace = resource["metadata"]["namespace"]
                identity = (namespace, resource["metadata"]["name"])
                self.assertNotIn(identity, found)
                self.kubectl(
                    "apply",
                    "-f",
                    "-",
                    resource={
                        "apiVersion": "v1",
                        "kind": "Namespace",
                        "metadata": {"name": namespace},
                    },
                )
                actual = self.admit(resource)
                self.assertEqual(actual["spec"]["timeZone"], "Europe/Berlin")
                self.assertEqual(
                    actual["spec"]["schedule"], resource["spec"]["schedule"]
                )
                found.add(identity)
        self.assertEqual(
            found,
            {
                ("backup-validation", "postgresql-restore-validation"),
                ("cloudnative-pg", "postgresql-r2-backup-budget-preview"),
                ("openbao", "openbao-snapshot"),
                ("shutdown-pod-preview", "shutdown-pod-preview"),
            },
        )

    def test_new_ephemeral_container_is_forced(self) -> None:
        resource = self.admit(pod(), dry_run=False)
        resource["spec"]["ephemeralContainers"] = [
            {
                "name": "debug",
                "image": "registry.k8s.io/pause:3.10",
                "targetContainerName": "main",
                "env": [
                    {
                        "name": "TZ",
                        "valueFrom": {
                            "configMapKeyRef": {"name": "tz-reference", "key": "TZ"}
                        },
                    }
                ],
            }
        ]
        actual = self.ephemeral(resource)
        self.assertEqual(
            tz_entries(actual["spec"]["ephemeralContainers"][0]), BERLIN_ENV
        )
        self.assertEqual(
            actual["spec"]["ephemeralContainers"][0]["targetContainerName"], "main"
        )

    def test_existing_ephemeral_environment_is_not_rewritten(self) -> None:
        resource = self.admit(pod(), dry_run=False)
        resource["spec"]["ephemeralContainers"] = [
            {
                "name": "legacy",
                "image": "registry.k8s.io/pause:3.10",
                "env": [{"name": "TZ", "value": "UTC"}],
            }
        ]
        # Seed a legacy debug container only in the isolated API, then restore
        # every binding before testing a newly appended container.
        with self.without_mutators(include_validation=True):
            deadline = time.monotonic() + 10
            while True:
                try:
                    witness = self.ephemeral(resource)
                    if tz_entries(witness["spec"]["ephemeralContainers"][0]) == [
                        {"name": "TZ", "value": "UTC"}
                    ]:
                        resource = self.ephemeral(resource, dry_run=False)
                        break
                except AssertionError as error:
                    if "Europe/Berlin" not in str(error):
                        raise
                if time.monotonic() >= deadline:
                    self.fail("Could not seed the isolated legacy fixture")
                time.sleep(0.2)
        resource["spec"]["ephemeralContainers"].append(
            {
                "name": "new-debug",
                "image": "registry.k8s.io/pause:3.10",
                "env": [{"name": "TZ", "value": "UTC"}],
            }
        )
        actual = self.ephemeral(resource)["spec"]["ephemeralContainers"]
        self.assertEqual(tz_entries(actual[0]), [{"name": "TZ", "value": "UTC"}])
        self.assertEqual(tz_entries(actual[1]), BERLIN_ENV)

    def test_final_validation_rejects_wrong_and_missing_tz_without_mutation(
        self,
    ) -> None:
        with self.without_mutators():
            for entries in (
                [],
                [{"name": "TZ", "value": "UTC"}],
                [
                    {
                        "name": "TZ",
                        "valueFrom": {
                            "configMapKeyRef": {"name": "tz-reference", "key": "TZ"}
                        },
                    }
                ],
                BERLIN_ENV + BERLIN_ENV,
            ):
                resource = pod()
                resource["spec"]["containers"][0]["env"] = entries
                self.assert_denied(resource)
            resource = cronjob()
            resource["spec"]["timeZone"] = "UTC"
            self.assert_denied(resource)
            resource["spec"].pop("timeZone")
            self.assert_denied(resource)
            good_pod = pod()
            good_pod["spec"]["containers"][0]["env"] = BERLIN_ENV
            self.admit(good_pod)
            good_cron = cronjob()
            good_cron["spec"]["timeZone"] = "Europe/Berlin"
            self.admit(good_cron)

    def test_final_validation_checks_init_and_ephemeral_containers(self) -> None:
        resource = self.admit(pod(), dry_run=False)
        with self.without_mutators():
            bad_init = pod()
            bad_init["spec"]["containers"][0]["env"] = BERLIN_ENV
            bad_init["spec"]["initContainers"] = [
                {"name": "init", "image": "registry.k8s.io/pause:3.10"}
            ]
            self.assert_denied(bad_init)
            resource["spec"]["ephemeralContainers"] = [
                {
                    "name": "debug",
                    "image": "registry.k8s.io/pause:3.10",
                    "env": [{"name": "TZ", "value": "UTC"}],
                }
            ]
            self.assert_denied(resource, ephemeral=True)


if __name__ == "__main__":
    unittest.main()
