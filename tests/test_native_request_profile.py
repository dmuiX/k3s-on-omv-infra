"""Exact-chart request consumption and resource-only change contracts."""

import unittest

from workload_contracts import Document, Repository

# Application, native value path, Deployment, container, CPU, RAM.
PROFILES = (
    (
        "argocd-config",
        "applicationSet.resources",
        "argocd-applicationset-controller",
        "applicationset-controller",
        "10m",
        "96Mi",
    ),
    (
        "argocd-config",
        "dex.resources",
        "argocd-dex-server",
        "dex-server",
        "10m",
        "144Mi",
    ),
    (
        "argocd-config",
        "dex.initImage.resources",
        "argocd-dex-server",
        "copyutil",
        "100m",
        "16Mi",
    ),
    (
        "argocd-config",
        "notifications.resources",
        "argocd-notifications-controller",
        "notifications-controller",
        "10m",
        "144Mi",
    ),
    ("argocd-config", "redis.resources", "argocd-redis", "redis", "10m", "48Mi"),
    (
        "cert-manager",
        "cainjector.resources",
        "cert-manager-cainjector",
        "cert-manager-cainjector",
        "10m",
        "144Mi",
    ),
    (
        "cert-manager",
        "webhook.resources",
        "cert-manager-webhook",
        "cert-manager-webhook",
        "10m",
        "96Mi",
    ),
    (
        "openbao",
        "injector.resources",
        "openbao-agent-injector",
        "sidecar-injector",
        "10m",
        "80Mi",
    ),
    (
        "kube-prometheus-stack",
        "prometheusOperator.resources",
        "kube-prometheus-stack-operator",
        "kube-prometheus-stack",
        "10m",
        "128Mi",
    ),
    (
        "kube-prometheus-stack",
        "kube-state-metrics.resources",
        "kube-prometheus-stack-kube-state-metrics",
        "kube-state-metrics",
        "10m",
        "224Mi",
    ),
    (
        "kube-prometheus-stack",
        "grafana.sidecar.resources",
        "kube-prometheus-stack-grafana",
        "grafana-sc-dashboard",
        "10m",
        "128Mi",
    ),
    (
        "kube-prometheus-stack",
        "grafana.sidecar.resources",
        "kube-prometheus-stack-grafana",
        "grafana-sc-datasources",
        "10m",
        "128Mi",
    ),
)
RELOADER = "prometheusOperator.prometheusConfigReloader.resources"


def values(repo: Repository, app: str) -> Document:
    """Return the canonical first chart source's inline values."""
    result: Document = repo.apps[app]["spec"]["sources"][0]["helm"]["valuesObject"]
    return result


def parent(root: Document, path: str) -> tuple[Document, str]:
    """Find a declared native path, failing rather than inventing missing keys."""
    parts = path.split(".")
    for part in parts[:-1]:
        root = root[part]
    return root, parts[-1]


def container(documents: list[Document], workload: str, name: str) -> Document:
    """Match both workload and container; Argo has another unrelated copyutil."""
    (deployment,) = [
        d
        for d in documents
        if d["kind"] == "Deployment" and d["metadata"]["name"] == workload
    ]
    pod = deployment["spec"]["template"]["spec"]
    result: Document
    (result,) = [
        c
        for c in pod["containers"] + pod.get("initContainers", [])
        if c["name"] == name
    ]
    return result


class NativeRequestProfileTest(unittest.TestCase):
    def test_exact_charts_consume_every_native_profile(self) -> None:
        repo = Repository()
        for app, path, workload, name, cpu, memory in PROFILES:
            with self.subTest(application=app, container=name):
                expected = {"cpu": cpu, "memory": memory}
                owner, key = parent(values(repo, app), path)
                self.assertEqual(owner[key], {"requests": expected})
                self.assertEqual(
                    container(repo.render(app), workload, name)["resources"][
                        "requests"
                    ],
                    expected,
                )

    def test_init_reloader_is_overridden_without_enlarging_the_steady_default(
        self,
    ) -> None:
        repo = Repository()
        docs = repo.render("kube-prometheus-stack")
        operator = container(
            docs, "kube-prometheus-stack-operator", "kube-prometheus-stack"
        )
        self.assertIn("--config-reloader-cpu-request=10m", operator["args"])
        self.assertIn("--config-reloader-memory-request=48Mi", operator["args"])
        (prometheus,) = [d for d in docs if d["kind"] == "Prometheus"]
        self.assertEqual(
            prometheus["spec"]["initContainers"],
            [
                {
                    "name": "init-config-reloader",
                    "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}},
                }
            ],
        )
        # Main container still dominates this init's scheduler CPU/RAM maximum.
        self.assertEqual(
            prometheus["spec"]["resources"]["requests"],
            {"cpu": "250m", "memory": "1Gi"},
        )

    def test_removing_profiles_changes_only_the_intended_requests(self) -> None:
        actual = Repository()
        baseline = Repository()
        paths = {(app, path) for app, path, *_ in PROFILES}
        paths.add(("kube-prometheus-stack", RELOADER))
        for app, path in paths:
            owner, key = parent(values(baseline, app), path)
            del owner[key]
        del values(baseline, "kube-prometheus-stack")["prometheus"]["prometheusSpec"][
            "initContainers"
        ]
        for app in sorted({row[0] for row in PROFILES}):
            with self.subTest(application=app):
                before = baseline.render(app)
                after = actual.render(app)
                for docs in (before, after):
                    for target_app, _, workload, name, *_ in PROFILES:
                        if target_app != app:
                            continue
                        target = container(docs, workload, name)
                        resources = target.get("resources", {})
                        resources.pop("requests", None)
                        if not resources:
                            target.pop("resources", None)
                    if app == "kube-prometheus-stack":
                        operator = container(
                            docs,
                            "kube-prometheus-stack-operator",
                            "kube-prometheus-stack",
                        )
                        operator["args"] = [
                            a
                            for a in operator["args"]
                            if not a.startswith(
                                (
                                    "--config-reloader-cpu-request=",
                                    "--config-reloader-memory-request=",
                                )
                            )
                        ]
                        (prometheus,) = [d for d in docs if d["kind"] == "Prometheus"]
                        prometheus["spec"].pop("initContainers", None)
                self.assertEqual(
                    after,
                    before,
                    "Limits, HA, security, identities and non-resource fields must remain unchanged",
                )

    def test_misspelled_native_key_does_not_pass_the_render_contract(self) -> None:
        repo = Repository()
        owner, key = parent(values(repo, "argocd-config"), "dex.initImage.resources")
        owner["resourcez"] = owner.pop(key)
        target = container(
            repo.render("argocd-config"), "argocd-dex-server", "copyutil"
        )
        # The chart silently falls back to Dex resources: this must not count as
        # adoption of the dedicated measured init profile.
        self.assertNotEqual(
            target["resources"]["requests"], {"cpu": "100m", "memory": "16Mi"}
        )
        self.assertEqual(
            target["resources"]["requests"], {"cpu": "10m", "memory": "144Mi"}
        )


if __name__ == "__main__":
    unittest.main()
