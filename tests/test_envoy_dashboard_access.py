"""Offline and native API contracts for the optional Envoy Dashboard grant."""

from copy import deepcopy
from pathlib import Path
import time
from typing import Any
import unittest

import yaml

import test_timezone_defaults as native
from workload_contracts import Repository, render_kustomization

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "01-bootstrap/istio-gateway/build"
RELEASE = ROOT / "02-controllers/tekton-pipelines/resources/release.yaml"
DASHBOARD = "system:serviceaccount:tekton-pipelines:tekton-dashboard"
ADMISSION = "tekton-dashboard-envoy-run"


class EnvoyDashboardAccessTest(unittest.TestCase):
    def test_tekton_crds_omit_the_api_default_for_unknown_fields(self) -> None:
        docs = yaml.safe_load_all(RELEASE.read_text())
        crds = [d for d in docs if d and d["kind"] == "CustomResourceDefinition"]
        self.assertTrue(crds)
        for crd in crds:
            with self.subTest(crd=crd["metadata"]["name"]):
                self.assertNotIn("preserveUnknownFields", crd["spec"])

    def test_setup_installs_reviewed_inputs_without_a_run_or_dashboard_grant(
        self,
    ) -> None:
        setup = yaml.safe_load((BUILD / "kustomization.yaml").read_text())
        self.assertNotIn("namespace", setup)
        self.assertEqual(
            setup["resources"], ["pipeline.yaml", "dashboard/admission.yaml"]
        )
        repo = Repository()
        sources = repo.apps["tekton-dashboard"]["spec"]["sources"]
        (source,) = [
            s for s in sources if s.get("path") == BUILD.relative_to(ROOT).as_posix()
        ]
        self.assertNotIn("directory", source)
        docs = repo.render("tekton-dashboard")
        self.assertFalse(any(d["kind"] in {"PipelineRun", "TaskRun"} for d in docs))
        build = render_kustomization(ROOT, BUILD)
        self.assertCountEqual(
            [
                (d["kind"], d["metadata"]["name"], d["metadata"].get("namespace"))
                for d in build
            ],
            [
                ("Pipeline", "envoy-backport-pilot", "ci-builds"),
                ("Task", "envoy-regression-build", "ci-builds"),
                ("ServiceAccount", "envoy-builder", "ci-builds"),
                ("ConfigMap", "envoy-build-inputs", "ci-builds"),
                ("NetworkPolicy", "public-build-downloads", "ci-builds"),
                ("ValidatingAdmissionPolicy", ADMISSION, None),
                ("ValidatingAdmissionPolicyBinding", ADMISSION, None),
            ],
        )
        (binding,) = [
            d
            for d in docs
            if d["kind"] == "ValidatingAdmissionPolicyBinding"
            and d["metadata"]["name"] == ADMISSION
        ]
        self.assertEqual(binding["spec"]["policyName"], ADMISSION)
        self.assertEqual(binding["spec"]["validationActions"], ["Deny"])
        (inputs,) = setup["configMapGenerator"]
        self.assertEqual(inputs["name"], "envoy-build-inputs")
        self.assertEqual(inputs["namespace"], "ci-builds")
        self.assertIs(inputs["options"]["disableNameSuffixHash"], True)
        self.assertEqual(
            set(inputs["files"]),
            {"lock.json", "48409.patch", "regression.patch", "inside.sh"},
        )
        for path in inputs["files"]:
            self.assertTrue((BUILD / path).is_file())

    def test_grant_is_dashboard_create_only_in_the_build_namespace(self) -> None:
        role, binding = yaml.safe_load_all(
            (BUILD / "dashboard/access.yaml").read_text()
        )
        self.assertEqual(role["kind"], "Role")
        self.assertEqual(binding["kind"], "RoleBinding")
        self.assertEqual(role["metadata"]["namespace"], "ci-builds")
        self.assertEqual(binding["metadata"]["namespace"], "ci-builds")
        self.assertEqual(
            role["rules"],
            [
                {
                    "apiGroups": ["tekton.dev"],
                    "resources": ["pipelineruns"],
                    "verbs": ["create"],
                }
            ],
        )
        self.assertEqual(binding["roleRef"]["kind"], "Role")
        self.assertEqual(binding["roleRef"]["name"], role["metadata"]["name"])
        self.assertEqual(
            binding["subjects"],
            [
                {
                    "kind": "ServiceAccount",
                    "name": "tekton-dashboard",
                    "namespace": "tekton-pipelines",
                }
            ],
        )

    def test_optional_guard_fails_closed_for_that_dashboard_identity(self) -> None:
        policy, binding = yaml.safe_load_all(
            (BUILD / "dashboard/admission.yaml").read_text()
        )
        self.assertEqual(policy["kind"], "ValidatingAdmissionPolicy")
        self.assertEqual(binding["kind"], "ValidatingAdmissionPolicyBinding")
        self.assertEqual(binding["spec"]["policyName"], policy["metadata"]["name"])
        self.assertEqual(binding["spec"]["validationActions"], ["Deny"])
        self.assertEqual(
            binding["spec"]["matchResources"]["namespaceSelector"]["matchLabels"],
            {"kubernetes.io/metadata.name": "ci-builds"},
        )
        spec = policy["spec"]
        self.assertEqual(spec["failurePolicy"], "Fail")
        self.assertEqual(spec["matchConstraints"]["matchPolicy"], "Equivalent")
        (rule,) = spec["matchConstraints"]["resourceRules"]
        self.assertEqual(rule["resources"], ["pipelineruns"])
        self.assertEqual(rule["operations"], ["CREATE"])
        self.assertIn(DASHBOARD, spec["matchConditions"][0]["expression"])


@unittest.skipUnless(native.TEST_CONTAINER, "An isolated native test API is required")
class NativeEnvoyDashboardAccessTest(unittest.TestCase):
    def test_run_validation_and_revocation(self) -> None:
        api = native.NativeTimezoneAdmissionTest
        api.require_isolated_api()
        k = api.kubectl
        crd = next(
            doc
            for doc in yaml.safe_load_all(RELEASE.read_text())
            if doc
            and doc["kind"] == "CustomResourceDefinition"
            and doc["metadata"]["name"] == "pipelineruns.tekton.dev"
        )
        # Keep the Tekton schemas but use identity conversion: this API has no webhook.
        crd["spec"]["conversion"] = {"strategy": "None"}
        k("apply", "--server-side", "-f", "-", resource=crd)
        k(
            "wait",
            "--for=condition=Established",
            "--timeout=10s",
            "crd/pipelineruns.tekton.dev",
        )
        k("create", "namespace", "ci-builds")
        for manifest in ("admission.yaml", "access.yaml"):
            k("apply", "-f", "-", text=(BUILD / "dashboard" / manifest).read_text())

        run = yaml.safe_load((BUILD / "run.yaml").read_text())
        del run["metadata"]["generateName"]
        run["metadata"]["name"] = "envoy-backport-dashboard"
        run["spec"]["timeouts"]["pipeline"] = "5h50m0s"
        run["spec"]["taskRunTemplate"]["podTemplate"][
            "automountServiceAccountToken"
        ] = False

        def submit(resource: dict[str, Any], *, dry_run: bool = True) -> str:
            args = ["create", f"--as={DASHBOARD}", "-f", "-"]
            if dry_run:
                args.append("--dry-run=server")
            return k(*args, resource=resource)

        def change(path: tuple[str, ...], value: object) -> dict[str, Any]:
            extra: dict[str, Any] = deepcopy(run)
            target = extra
            for field in path[:-1]:
                target = target[field]
            target[path[-1]] = value
            return extra

        def wait_for_denial(
            resource: dict[str, Any], expected: str, interim: str
        ) -> None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    submit(resource)
                except AssertionError as error:
                    if expected in str(error):
                        return
                    if interim not in str(error):
                        raise
                time.sleep(0.2)
            self.fail(f"Expected {expected} to deny the dry-run")

        wait_for_denial(
            change(("metadata", "name"), "unapproved-run"), ADMISSION, "forbidden"
        )
        submit(run)
        overrides = {
            ("metadata", "annotations"): {"example.org/extra": "true"},
            ("spec", "pipelineRef", "name"): "unapproved-pipeline",
            ("spec", "taskRunTemplate", "serviceAccountName"): "default",
            ("spec", "taskRunTemplate", "podTemplate", "hostNetwork"): True,
            ("spec", "taskRunTemplate", "podTemplate", "nodeSelector"): {},
            ("spec", "timeouts", "pipeline"): "6h0m0s",
            ("spec", "workspaces"): [{"name": "other", "emptyDir": {}}],
        }
        for path, value in overrides.items():
            with self.subTest(path=path), self.assertRaisesRegex(
                AssertionError, ADMISSION
            ):
                submit(change(path, value))
        with self.assertRaisesRegex(AssertionError, "must use Tekton API v1"):
            submit(
                {
                    **run,
                    "apiVersion": "tekton.dev/v1beta1",
                    "spec": {"pipelineRef": run["spec"]["pipelineRef"]},
                }
            )
        for verb, resource, namespace in (
            ("create", "tasks.tekton.dev", "ci-builds"),
            ("create", "pipelineruns.tekton.dev", "default"),
            ("patch", "pipelineruns.tekton.dev", "ci-builds"),
            ("delete", "pipelineruns.tekton.dev", "ci-builds"),
            ("get", "secrets", "ci-builds"),
        ):
            with self.subTest(verb=verb, resource=resource), self.assertRaises(
                AssertionError
            ):
                k(
                    "auth",
                    "can-i",
                    verb,
                    resource,
                    f"--as={DASHBOARD}",
                    "-n",
                    namespace,
                    "--quiet",
                )
        submit(run, dry_run=False)
        with self.assertRaisesRegex(AssertionError, "already exists"):
            submit(run, dry_run=False)
        k("delete", "-n", "ci-builds", "rolebinding", ADMISSION)
        wait_for_denial(run, "forbidden", "already exists")
