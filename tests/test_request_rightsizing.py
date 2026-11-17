"""Measured native request reductions must preserve limits and all other fields."""

import json
import unittest

from workload_contracts import Document, Repository

# Application, chart, resources path, workload, container, old CPU/RAM, new CPU/RAM.
CHANGES = (
    (
        "argocd-config",
        "argo-cd",
        "repoServer.resources",
        "argocd-repo-server",
        "repo-server",
        "100m",
        "128Mi",
        "50m",
        "128Mi",
    ),
    (
        "argocd-config",
        "argo-cd",
        "repoServer.resources",
        "argocd-repo-server",
        "copyutil",
        "100m",
        "128Mi",
        "50m",
        "128Mi",
    ),
    (
        "argocd-config",
        "argo-cd",
        "server.resources",
        "argocd-server",
        "server",
        "50m",
        "128Mi",
        "20m",
        "128Mi",
    ),
    (
        "istio-core",
        "ztunnel",
        "resources",
        "ztunnel",
        "istio-proxy",
        "200m",
        "128Mi",
        "150m",
        "64Mi",
    ),
    (
        "istio-core",
        "istiod",
        "resources",
        "istiod",
        "discovery",
        "100m",
        "256Mi",
        "25m",
        "224Mi",
    ),
    (
        "istio-core",
        "cni",
        "resources",
        "istio-cni-node",
        "install-cni",
        "100m",
        "100Mi",
        "25m",
        "100Mi",
    ),
    (
        "barman-cloud",
        "plugin-barman-cloud",
        "resources",
        "barman-cloud-plugin-barman-cloud",
        "barman-cloud",
        "50m",
        "96Mi",
        "20m",
        "80Mi",
    ),
    (
        "cert-manager",
        "cert-manager",
        "resources",
        "cert-manager",
        "cert-manager-controller",
        "50m",
        "64Mi",
        "10m",
        "64Mi",
    ),
    (
        "crossplane",
        "crossplane",
        "resourcesRBACManager",
        "crossplane-rbac-manager",
        "crossplane",
        "100m",
        "256Mi",
        "10m",
        "64Mi",
    ),
    (
        "crossplane",
        "crossplane",
        "resourcesRBACManager",
        "crossplane-rbac-manager",
        "crossplane-init",
        "100m",
        "256Mi",
        "10m",
        "64Mi",
    ),
    (
        "k8up",
        "k8up",
        "resources",
        "k8up",
        "k8up-operator",
        "50m",
        "96Mi",
        "10m",
        "80Mi",
    ),
    (
        "openbao",
        "openbao",
        "server.resources",
        "openbao",
        "openbao",
        "100m",
        "256Mi",
        "100m",
        "224Mi",
    ),
    (
        "vault-secrets-operator",
        "vault-secrets-operator",
        "controller.manager.resources",
        "vault-secrets-operator-controller-manager",
        "manager",
        "50m",
        "96Mi",
        "20m",
        "96Mi",
    ),
)


def resources(repo: Repository, app: str, chart: str, path: str) -> Document:
    """Find the exact chart source; avoid the unrelated Istio source scopes."""
    source = next(
        s for s in repo.apps[app]["spec"]["sources"] if s.get("chart") == chart
    )
    result: Document = source["helm"]["valuesObject"]
    for part in path.split("."):
        result = result[part]
    return result


def target(docs: list[Document], workload: str, name: str) -> Document:
    """Resolve a unique named container, including native shared init consumers."""
    (obj,) = [
        d
        for d in docs
        if d["kind"] in {"Deployment", "DaemonSet", "StatefulSet"}
        and d["metadata"]["name"] == workload
    ]
    pod = obj["spec"]["template"]["spec"]
    result: Document
    (result,) = [
        c
        for c in pod["containers"] + pod.get("initContainers", [])
        if c["name"] == name
    ]
    return result


class RequestRightsizingTest(unittest.TestCase):
    def test_every_reduction_reaches_its_real_container_and_shared_init(self) -> None:
        repo = Repository()
        for app, chart, path, workload, name, _, _, cpu, memory in CHANGES:
            with self.subTest(application=app, container=name):
                expected = {"cpu": cpu, "memory": memory}
                self.assertEqual(
                    resources(repo, app, chart, path)["requests"], expected
                )
                self.assertEqual(
                    target(repo.render(app), workload, name)["resources"]["requests"],
                    expected,
                )

    def test_exact_chart_delta_is_requests_only_not_limits_ha_or_security(self) -> None:
        actual, baseline = Repository(), Repository()
        for app, chart, path, _, _, cpu, memory, _, _ in CHANGES:
            if path == "resourcesRBACManager":
                source = next(
                    s
                    for s in baseline.apps[app]["spec"]["sources"]
                    if s.get("chart") == chart
                )
                source["helm"]["valuesObject"].pop(path, None)
            else:
                resources(baseline, app, chart, path)["requests"] = {
                    "cpu": cpu,
                    "memory": memory,
                }
        for app in sorted({row[0] for row in CHANGES}):
            with self.subTest(application=app):
                before, after = baseline.render(app), actual.render(app)
                for docs in (before, after):
                    for target_app, _, _, workload, name, *_ in CHANGES:
                        if target_app == app:
                            target(docs, workload, name)["resources"].pop(
                                "requests", None
                            )
                    if app == "istio-core":
                        # The pinned istiod chart echoes its values as JSON.
                        # Normalize ONLY the same two request fields, not the
                        # ConfigMap or unrelated injection/security values.
                        (echo,) = [
                            d
                            for d in docs
                            if d["kind"] == "ConfigMap"
                            and d["metadata"]["name"] == "values"
                        ]
                        for key in ("original-values", "merged-values"):
                            value = json.loads(echo["data"][key])
                            value["resources"].pop("requests")
                            echo["data"][key] = json.dumps(value, sort_keys=True)
                self.assertEqual(
                    after, before, "Only the named native request maps may differ"
                )

    def test_shared_init_requests_cannot_hide_the_rbac_savings(self) -> None:
        docs = Repository().render("crossplane")
        main = target(docs, "crossplane-rbac-manager", "crossplane")
        init = target(docs, "crossplane-rbac-manager", "crossplane-init")
        self.assertEqual(main["resources"], init["resources"])
        self.assertEqual(
            main["resources"]["limits"], {"cpu": "100m", "memory": "512Mi"}
        )
        self.assertEqual(main["resources"]["requests"]["memory"], "64Mi")
        controller = target(docs, "crossplane", "crossplane")
        self.assertEqual(
            controller["resources"]["requests"], {"cpu": "100m", "memory": "256Mi"}
        )


if __name__ == "__main__":
    unittest.main()
