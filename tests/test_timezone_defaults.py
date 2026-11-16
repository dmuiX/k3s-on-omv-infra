"""Offline contracts and opt-in admission tests against an isolated K3s API."""

from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "01-bootstrap/timezone-defaults"
POLICY_FILE = COMPONENT / "resources/defaults.yaml"
DOCUMENTS = list(yaml.safe_load_all(POLICY_FILE.read_text()))
POLICIES = {
    doc["metadata"]["name"]: doc
    for doc in DOCUMENTS
    if doc["kind"] == "MutatingAdmissionPolicy"
}
TEST_CONTAINER = os.environ.get("TIMEZONE_TEST_CONTAINER", "")


def pod(containers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "timezone-admission-probe"},
        "spec": {
            "automountServiceAccountToken": False,
            "serviceAccountName": "timezone-admission-test",
            "restartPolicy": "Never",
            "containers": containers
            or [{"name": "main", "image": "registry.k8s.io/pause:3.10"}],
        },
    }


def cronjob() -> dict[str, Any]:
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
    return [entry for entry in container.get("env", []) if entry["name"] == "TZ"]


class TimezoneContractTest(unittest.TestCase):
    def test_only_native_policies_and_bindings(self) -> None:
        self.assertEqual(len(DOCUMENTS), 4)
        self.assertEqual(len(POLICIES), 2)
        bindings = [
            doc for doc in DOCUMENTS if doc["kind"] == "MutatingAdmissionPolicyBinding"
        ]
        self.assertEqual(len(bindings), 2)
        self.assertEqual(
            {binding["spec"]["policyName"] for binding in bindings}, set(POLICIES)
        )
        for document in DOCUMENTS:
            self.assertEqual(document["apiVersion"], "admissionregistration.k8s.io/v1")
        for policy in POLICIES.values():
            self.assertEqual(policy["spec"]["failurePolicy"], "Ignore")
            self.assertEqual(policy["spec"]["reinvocationPolicy"], "IfNeeded")
            self.assertEqual(
                policy["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "-1"
            )

    def test_admission_scope(self) -> None:
        for name, group, resource, operations in (
            ("default-cronjob-timezone", "batch", "cronjobs", ["CREATE", "UPDATE"]),
            ("default-container-timezone", "", "pods", ["CREATE"]),
        ):
            spec = POLICIES[name]["spec"]
            self.assertEqual(
                spec["matchConstraints"],
                {
                    "resourceRules": [
                        {
                            "apiGroups": [group],
                            "apiVersions": ["v1"],
                            "operations": operations,
                            "resources": [resource],
                        }
                    ]
                },
            )
        for binding in (d for d in DOCUMENTS if d["kind"].endswith("Binding")):
            self.assertNotIn("matchResources", binding["spec"])

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
        resources = yaml.safe_load((ROOT / "kustomization.yaml").read_text())[
            "resources"
        ]
        self.assertIn("01-bootstrap/timezone-defaults/app.yml", resources)

    def test_git_owned_cronjobs_have_explicit_timezone_to_avoid_argo_drift(
        self,
    ) -> None:
        found = []
        for path in ROOT.glob("[0-9][0-9]-*/**/*"):
            if path.suffix not in {".yaml", ".yml"}:
                continue
            for document in yaml.safe_load_all(path.read_text()):
                if isinstance(document, dict) and document.get("kind") == "CronJob":
                    found.append(document["metadata"]["name"])
                    self.assertEqual(
                        document["spec"]["timeZone"], "Europe/Berlin", str(path)
                    )
        self.assertEqual(
            set(found),
            {
                "openbao-snapshot",
                "postgresql-size-retention",
                "postgresql-restore-validation",
            },
        )

    def test_berlin_trim_wall_time_in_winter_and_summer(self) -> None:
        berlin = ZoneInfo("Europe/Berlin")
        for month, utc_hour in ((1, 17), (7, 16)):
            local = datetime(2027, month, 16, 18, 35, tzinfo=berlin)
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
    def setUpClass(cls) -> None:
        # Never use the operator's kubectl context, mounted kubeconfig or a
        # production API. These checks precede every mutating test command.
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
        cls.kubectl("apply", "-f", "-", text=POLICY_FILE.read_text())
        # API readiness can precede the default-ServiceAccount controller.
        # Use a dedicated, tokenless fixture rather than racing that controller.
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
                tz_entries(witness["spec"]["containers"][0])
                == [{"name": "TZ", "value": "Europe/Berlin"}]
                and cls.admit(cronjob())["spec"].get("timeZone") == "Europe/Berlin"
            ):
                break
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "Native admission did not activate both timezone defaults"
                )
            time.sleep(0.2)

    def test_pod_default_preserves_existing_fields_and_other_environment(self) -> None:
        container = {
            "name": "main",
            "image": "registry.k8s.io/pause:3.10",
            "args": ["original"],
            "env": [{"name": "KEEP", "value": "original"}],
        }
        admitted = self.admit(pod([container]))["spec"]["containers"][0]
        self.assertEqual(
            tz_entries(admitted), [{"name": "TZ", "value": "Europe/Berlin"}]
        )
        self.assertEqual(admitted["args"], container["args"])
        self.assertEqual(admitted["image"], container["image"])
        self.assertIn(container["env"][0], admitted["env"])

    def test_explicit_literal_empty_and_valuefrom_timezone_are_preserved(self) -> None:
        for entry in (
            {"name": "TZ", "value": "UTC"},
            {"name": "TZ", "value": ""},
            {
                "name": "TZ",
                "valueFrom": {"secretKeyRef": {"name": "tz-reference", "key": "TZ"}},
            },
        ):
            with self.subTest(entry=entry):
                resource = pod()
                resource["spec"]["containers"][0]["env"] = [entry]
                actual = self.admit(resource)["spec"]["containers"][0]
                # The API may omit an explicitly empty value; no default was added.
                self.assertEqual(len(tz_entries(actual)), 1)
                self.assertNotEqual(tz_entries(actual)[0].get("value"), "Europe/Berlin")
                if "valueFrom" in entry:
                    self.assertEqual(
                        tz_entries(actual)[0]["valueFrom"], entry["valueFrom"]
                    )

    def test_envfrom_is_not_overridden_or_resolved(self) -> None:
        for field in ("secretRef", "configMapRef"):
            with self.subTest(field=field):
                resource = pod()
                resource["spec"]["containers"][0]["envFrom"] = [
                    {field: {"name": "tz-reference"}}
                ]
                actual = self.admit(resource)["spec"]["containers"][0]
                self.assertEqual(tz_entries(actual), [])
                self.assertEqual(
                    actual["envFrom"], resource["spec"]["containers"][0]["envFrom"]
                )

    def test_empty_envfrom_still_gets_default(self) -> None:
        resource = pod()
        resource["spec"]["containers"][0]["envFrom"] = []
        actual = self.admit(resource)["spec"]["containers"][0]
        self.assertEqual(tz_entries(actual), [{"name": "TZ", "value": "Europe/Berlin"}])

    def test_mixed_containers_and_init_sidecars_preserve_order_and_explicit_choices(
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
                "name": "utc-init",
                "image": "registry.k8s.io/pause:3.10",
                "env": [{"name": "TZ", "value": "UTC"}],
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
        for container in actual["initContainers"][:2] + actual["containers"][:1]:
            self.assertEqual(
                tz_entries(container), [{"name": "TZ", "value": "Europe/Berlin"}]
            )
        self.assertEqual(
            tz_entries(actual["initContainers"][2]), [{"name": "TZ", "value": "UTC"}]
        )
        self.assertEqual(tz_entries(actual["initContainers"][3]), [])
        self.assertEqual(
            tz_entries(actual["containers"][1]), [{"name": "TZ", "value": "UTC"}]
        )
        self.assertEqual(actual["initContainers"][1]["restartPolicy"], "Always")

    def test_reapplication_is_idempotent(self) -> None:
        resource = pod()
        resource["spec"] = self.admit(resource)["spec"]
        actual = self.admit(resource)
        self.assertEqual(
            tz_entries(actual["spec"]["containers"][0]),
            [{"name": "TZ", "value": "Europe/Berlin"}],
        )

    def test_windows_pod_is_not_modified(self) -> None:
        resource = pod()
        resource["spec"]["os"] = {"name": "windows"}
        actual = self.admit(resource)["spec"]["containers"][0]
        self.assertEqual(tz_entries(actual), [])

    def test_cronjob_missing_or_empty_timezone_gets_berlin(self) -> None:
        for value in (None, ""):
            resource = cronjob()
            if value is not None:
                resource["spec"]["timeZone"] = value
            actual = self.admit(resource)["spec"]
            self.assertEqual(actual["timeZone"], "Europe/Berlin")
            self.assertEqual(actual["schedule"], resource["spec"]["schedule"])
            self.assertNotIn(
                "env",
                actual["jobTemplate"]["spec"]["template"]["spec"]["containers"][0],
            )

    def test_explicit_cronjob_timezone_is_preserved(self) -> None:
        for value in ("UTC", "America/New_York", "Europe/Berlin"):
            resource = cronjob()
            resource["spec"]["timeZone"] = value
            self.assertEqual(self.admit(resource)["spec"]["timeZone"], value)

    def test_cronjob_update_defaults_missing_timezone(self) -> None:
        resource = self.admit(cronjob(), dry_run=False)
        resource["spec"].pop("timeZone")
        self.assertEqual(
            self.admit(resource, operation="replace")["spec"]["timeZone"],
            "Europe/Berlin",
        )


if __name__ == "__main__":
    unittest.main()
