"""Measured native RAM caps, memory-only deltas and owned API boundaries."""

import copy
import json
import unittest

import test_timezone_defaults as timezone
from test_native_request_profile import RELOADER, values
from test_request_rightsizing import resources, target
from workload_contracts import Document, Repository

# Application, chart, native resources path, workload, container, old cap, cap.
CAPS = (
    (
        "argocd-config",
        "argo-cd",
        "applicationSet.resources",
        "argocd-applicationset-controller",
        "applicationset-controller",
        None,
        "256Mi",
    ),
    (
        "argocd-config",
        "argo-cd",
        "dex.resources",
        "argocd-dex-server",
        "dex-server",
        None,
        "256Mi",
    ),
    (
        "argocd-config",
        "argo-cd",
        "notifications.resources",
        "argocd-notifications-controller",
        "notifications-controller",
        None,
        "256Mi",
    ),
    (
        "argocd-config",
        "argo-cd",
        "redis.resources",
        "argocd-redis",
        "redis",
        None,
        "128Mi",
    ),
    (
        "cert-manager",
        "cert-manager",
        "cainjector.resources",
        "cert-manager-cainjector",
        "cert-manager-cainjector",
        None,
        "256Mi",
    ),
    (
        "cert-manager",
        "cert-manager",
        "webhook.resources",
        "cert-manager-webhook",
        "cert-manager-webhook",
        None,
        "256Mi",
    ),
    (
        "openbao",
        "openbao",
        "injector.resources",
        "openbao-agent-injector",
        "sidecar-injector",
        None,
        "128Mi",
    ),
    (
        "kube-prometheus-stack",
        "kube-prometheus-stack",
        "prometheusOperator.resources",
        "kube-prometheus-stack-operator",
        "kube-prometheus-stack",
        None,
        "256Mi",
    ),
    (
        "kube-prometheus-stack",
        "kube-prometheus-stack",
        "kube-state-metrics.resources",
        "kube-prometheus-stack-kube-state-metrics",
        "kube-state-metrics",
        None,
        "384Mi",
    ),
    (
        "kube-prometheus-stack",
        "kube-prometheus-stack",
        "grafana.sidecar.resources",
        "kube-prometheus-stack-grafana",
        "grafana-sc-dashboard",
        None,
        "256Mi",
    ),
    (
        "kube-prometheus-stack",
        "kube-prometheus-stack",
        "grafana.sidecar.resources",
        "kube-prometheus-stack-grafana",
        "grafana-sc-datasources",
        None,
        "256Mi",
    ),
    ("istio-core", "ztunnel", "resources", "ztunnel", "istio-proxy", "1Gi", "256Mi"),
    (
        "crossplane",
        "crossplane",
        "resourcesRBACManager",
        "crossplane-rbac-manager",
        "crossplane",
        "512Mi",
        "128Mi",
    ),
    (
        "crossplane",
        "crossplane",
        "resourcesRBACManager",
        "crossplane-rbac-manager",
        "crossplane-init",
        "512Mi",
        "128Mi",
    ),
)


def init_override(repo: Repository) -> Document:
    """Require the existing unique named resource-only Prometheus init patch."""
    result: Document
    (result,) = values(repo, "kube-prometheus-stack")["prometheus"]["prometheusSpec"][
        "initContainers"
    ]
    return result


def remove_memory_limit(resource: Document) -> None:
    """Normalize only the allowed field, retaining CPU/other caps and requests."""
    limits = resource.get("limits", {})
    limits.pop("memory", None)
    if not limits:
        resource.pop("limits", None)


class MemoryLimitsContractTest(unittest.TestCase):
    def test_every_cap_reaches_its_named_container_including_shared_init(self) -> None:
        repo = Repository()
        for app, chart, path, workload, name, _, cap in CAPS:
            with self.subTest(application=app, container=name):
                native = resources(repo, app, chart, path)
                self.assertEqual(native.get("limits", {}).get("memory"), cap)
                self.assertEqual(
                    target(repo.render(app), workload, name)["resources"]["limits"][
                        "memory"
                    ],
                    cap,
                )
                self.assertTrue(native["requests"]["cpu"])
                self.assertTrue(native["requests"]["memory"])

    def test_reloader_default_is_paired_with_the_larger_named_init_cap(self) -> None:
        repo = Repository()
        docs = repo.render("kube-prometheus-stack")
        operator = target(
            docs, "kube-prometheus-stack-operator", "kube-prometheus-stack"
        )
        for flag in (
            "--config-reloader-memory-limit=128Mi",
            "--config-reloader-cpu-limit=0",
            "--config-reloader-cpu-request=10m",
            "--config-reloader-memory-request=48Mi",
        ):
            self.assertIn(flag, operator["args"])
        (prom,) = [d for d in docs if d["kind"] == "Prometheus"]
        self.assertEqual(
            prom["spec"]["initContainers"],
            [
                {
                    "name": "init-config-reloader",
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"memory": "512Mi"},
                    },
                }
            ],
        )
        self.assertEqual(
            prom["spec"]["resources"]["limits"], {"memory": "2Gi"}
        )
        (alertmanager,) = [d for d in docs if d["kind"] == "Alertmanager"]
        self.assertEqual(alertmanager["spec"]["replicas"], 0)

    def test_whole_rendered_delta_is_only_the_allowlisted_memory_caps(self) -> None:
        actual, baseline = Repository(), Repository()
        for app, chart, path, _, _, old, _ in CAPS:
            native = resources(baseline, app, chart, path)
            if old is None:
                remove_memory_limit(native)
            else:
                native.setdefault("limits", {})["memory"] = old
        remove_memory_limit(
            resources(
                baseline, "kube-prometheus-stack", "kube-prometheus-stack", RELOADER
            )
        )
        remove_memory_limit(init_override(baseline)["resources"])
        for app in sorted({row[0] for row in CAPS}):
            with self.subTest(application=app):
                before, after = baseline.render(app), actual.render(app)
                for docs in (before, after):
                    for target_app, _, _, workload, name, _, _ in CAPS:
                        if target_app == app:
                            remove_memory_limit(
                                target(docs, workload, name)["resources"]
                            )
                    if app == "kube-prometheus-stack":
                        operator = target(
                            docs,
                            "kube-prometheus-stack-operator",
                            "kube-prometheus-stack",
                        )
                        operator["args"] = [
                            (
                                "--config-reloader-memory-limit=0"
                                if a.startswith("--config-reloader-memory-limit=")
                                else a
                            )
                            for a in operator["args"]
                        ]
                        (prom,) = [d for d in docs if d["kind"] == "Prometheus"]
                        remove_memory_limit(
                            prom["spec"]["initContainers"][0]["resources"]
                        )
                self.assertEqual(
                    after,
                    before,
                    "Requests, CPU caps, versions, HA, security and every other resource field must stay identical",
                )

    def test_misspelled_native_resources_key_does_not_count_as_a_cap(self) -> None:
        repo = Repository()
        native = values(repo, "argocd-config")["applicationSet"]
        native["resourcez"] = native.pop("resources")
        actual = target(
            repo.render("argocd-config"),
            "argocd-applicationset-controller",
            "applicationset-controller",
        )
        self.assertIsNone(actual.get("resources", {}).get("limits", {}).get("memory"))


@unittest.skipUnless(timezone.TEST_CONTAINER, "Owned isolated native API required")
class NativeMemoryLimitApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Exact owned-container/no-bind/no-network guard; never host kubeconfig.
        timezone.NativeTimezoneAdmissionTest.setUpClass()

    def merged_reloader(self, omit_init_cap: bool = False) -> tuple[Document, Document]:
        """Use kubectl's real strategic merge, not a Python operator simulation."""
        repo = Repository()
        default = resources(
            repo, "kube-prometheus-stack", "kube-prometheus-stack", RELOADER
        )
        original = {
            "name": "init-config-reloader",
            "image": "registry.k8s.io/pause:3.10",
            "args": ["synthetic-generated-args-unchanged"],
            "resources": copy.deepcopy(default),
        }
        resource = timezone.pod(
            [
                {
                    "name": "config-reloader",
                    "image": "registry.k8s.io/pause:3.10",
                    "resources": copy.deepcopy(default),
                }
            ]
        )
        resource["metadata"] = {"name": "memory-limit-reloader-probe"}
        resource["spec"]["initContainers"] = [copy.deepcopy(original)]
        override = copy.deepcopy(init_override(repo))
        if omit_init_cap:
            override["resources"].pop("limits", None)
        merged: Document = json.loads(
            timezone.NativeTimezoneAdmissionTest.kubectl(
                "patch",
                "--local=true",
                "--type=strategic",
                "-f",
                "-",
                "-p",
                json.dumps({"spec": {"initContainers": [override]}}),
                "-o",
                "json",
                resource=resource,
            )
        )
        return merged, original

    def test_native_named_merge_and_admission_preserve_init_and_requests(self) -> None:
        merged, original = self.merged_reloader()
        init = merged["spec"]["initContainers"][0]
        self.assertEqual(init["args"], original["args"])
        self.assertEqual(init["image"], original["image"])
        self.assertEqual(
            init["resources"],
            {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"memory": "512Mi"},
            },
        )
        admitted = timezone.NativeTimezoneAdmissionTest.admit(merged)
        self.assertEqual(
            admitted["spec"]["initContainers"][0]["resources"], init["resources"]
        )
        self.assertEqual(
            admitted["spec"]["containers"][0]["resources"],
            merged["spec"]["containers"][0]["resources"],
        )

    def test_inherited_small_default_without_init_limit_is_rejected(self) -> None:
        merged, _ = self.merged_reloader(omit_init_cap=True)
        self.assertEqual(
            merged["spec"]["initContainers"][0]["resources"]["limits"]["memory"],
            "128Mi",
        )
        with self.assertRaisesRegex(
            AssertionError, "must be less than or equal to memory limit"
        ):
            timezone.NativeTimezoneAdmissionTest.admit(merged)

    def test_api_keeps_explicit_requests_and_existing_cpu_caps(self) -> None:
        repo = Repository()
        containers: list[Document] = []
        for index, (app, chart, path, _, _, _, cap) in enumerate(CAPS):
            native = resources(repo, app, chart, path)
            self.assertEqual(native["limits"]["memory"], cap)
            containers.append(
                {
                    "name": f"profile-{index}",
                    "image": "registry.k8s.io/pause:3.10",
                    "resources": copy.deepcopy(native),
                }
            )
        admitted = timezone.NativeTimezoneAdmissionTest.admit(timezone.pod(containers))
        self.assertEqual(
            [c["resources"] for c in admitted["spec"]["containers"]],
            [c["resources"] for c in containers],
        )


if __name__ == "__main__":
    unittest.main()
