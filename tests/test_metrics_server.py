"""The resource Metrics API has one declarative owner, independent of OpenBao."""

import json
from pathlib import Path
import unittest

import yaml

from workload_contracts import Repository

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "01-bootstrap/metrics-server/app.yml"


class MetricsServerTest(unittest.TestCase):
    def test_early_official_chart_has_no_private_or_namespace_dependency(self):
        app = yaml.safe_load(APP.read_text())
        self.assertEqual(app["metadata"]["name"], "metrics-server")
        self.assertEqual(app["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "-6")
        self.assertEqual(app["spec"]["destination"]["namespace"], "kube-system")
        source = app["spec"]["source"]
        self.assertEqual(source["repoURL"], "https://kubernetes-sigs.github.io/metrics-server/")
        self.assertEqual(source["chart"], "metrics-server")
        self.assertEqual(source["targetRevision"], "3.14.0")
        self.assertNotIn("valueFiles", source["helm"])
        self.assertNotIn("managedNamespaceMetadata", app["spec"]["syncPolicy"])
        self.assertNotIn("CreateNamespace=true", app["spec"]["syncPolicy"]["syncOptions"])
        repo = Repository()
        istio_wave = int(repo.apps["istio-core"]["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
        self.assertLess(-6, istio_wave, "Istiod's resource HPA must not block the Metrics API")

    def test_native_chart_preserves_requests_and_excludes_the_pi(self):
        docs = Repository().render("metrics-server")
        deployment, = [d for d in docs if d["kind"] == "Deployment"]
        self.assertEqual(deployment["metadata"]["name"], "metrics-server")
        self.assertEqual(deployment["spec"]["replicas"], 2)
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/os": "linux", "compute-class": "standard"})
        self.assertFalse(any(t.get("key") == "CriticalAddonsOnly" for t in pod.get("tolerations", [])))
        container, = pod["containers"]
        self.assertEqual(container["image"], "registry.k8s.io/metrics-server/metrics-server:v0.9.0")
        self.assertEqual(container["resources"], {"requests": {"cpu": "100m", "memory": "70Mi"}})
        self.assertTrue(container["livenessProbe"])
        self.assertTrue(container["readinessProbe"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual(container["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertTrue(container["securityContext"]["runAsNonRoot"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertNotEqual(pod["serviceAccountName"], "default")

    def test_replicas_require_distinct_hosts_and_can_roll_without_a_third_host(self):
        docs = Repository().render("metrics-server")
        deployment, = [d for d in docs if d["kind"] == "Deployment"]
        spec = deployment["spec"]
        pod = spec["template"]["spec"]
        terms = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual(terms, [{
            "topologyKey": "kubernetes.io/hostname",
            "labelSelector": {"matchLabels": spec["selector"]["matchLabels"]},
        }])
        self.assertEqual(spec["strategy"], {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
        })

    def test_native_pdb_keeps_one_replica_available_for_voluntary_disruptions(self):
        docs = Repository().render("metrics-server")
        deployment, = [d for d in docs if d["kind"] == "Deployment"]
        budget, = [d for d in docs if d["kind"] == "PodDisruptionBudget"]
        self.assertEqual(budget["apiVersion"], "policy/v1")
        self.assertEqual(budget["metadata"]["name"], "metrics-server")
        self.assertEqual(budget["metadata"]["namespace"], "kube-system")
        self.assertEqual(budget["spec"]["minAvailable"], 1)
        self.assertNotIn("maxUnavailable", budget["spec"])
        self.assertEqual(budget["spec"]["selector"], deployment["spec"]["selector"])

    def test_misspelled_ha_values_do_not_silently_satisfy_the_render_contract(self):
        for field in ("affinity", "updateStrategy", "podDisruptionBudget"):
            with self.subTest(field=field):
                repo = Repository()
                values = repo.apps["metrics-server"]["spec"]["source"]["helm"]["valuesObject"]
                values[field + "Typo"] = values.pop(field)
                docs = repo.render("metrics-server")
                deployment = next(d for d in docs if d["kind"] == "Deployment")
                if field == "affinity":
                    self.assertNotIn("affinity", deployment["spec"]["template"]["spec"])
                elif field == "updateStrategy":
                    self.assertNotIn("strategy", deployment["spec"])
                else:
                    self.assertFalse(any(d["kind"] == "PodDisruptionBudget" for d in docs))

    def test_chart_owns_the_single_metrics_api_and_its_rbac_service_chain(self):
        docs = Repository().render("metrics-server")
        api, = [d for d in docs if d["kind"] == "APIService"]
        self.assertEqual(api["metadata"]["name"], "v1beta1.metrics.k8s.io")
        self.assertEqual(api["spec"]["group"], "metrics.k8s.io")
        service = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == api["spec"]["service"]["name"])
        self.assertEqual(service["metadata"]["namespace"], "kube-system")
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        self.assertTrue(all(labels.get(k) == v for k, v in service["spec"]["selector"].items()))
        account = deployment["spec"]["template"]["spec"]["serviceAccountName"]
        self.assertTrue(any(d["kind"] == "ServiceAccount" and d["metadata"]["name"] == account for d in docs))
        self.assertTrue(any(d["kind"] == "ClusterRoleBinding" and any(s.get("name") == account for s in d["subjects"]) for d in docs))
        self.assertTrue(any(d["kind"] == "RoleBinding" and d["roleRef"]["name"] == "extension-apiserver-authentication-reader" for d in docs))
        self.assertFalse(any(d["kind"] in ("Secret", "Certificate", "Issuer", "Namespace") for d in docs))
        self.assertFalse((APP.parent / "resources").exists(), "No second APIService or custom Deployment")

    def test_kubelet_certificate_verification_and_native_transport_remain(self):
        docs = Repository().render("metrics-server")
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn("--kubelet-use-node-status-port", args)
        self.assertIn("--metric-resolution=15s", args)
        self.assertIn("--kubelet-preferred-address-types=InternalIP,ExternalIP,Hostname", args)
        self.assertFalse(any(a.startswith("--kubelet-insecure-tls") for a in args))
        # Match the packaged K3s APIService's runtime self-signed serving TLS.
        api = next(d for d in docs if d["kind"] == "APIService")
        self.assertTrue(api["spec"]["insecureSkipTLSVerify"])

    def test_chart_coordinate_is_integrity_pinned_and_application_is_registered(self):
        lock = json.loads((ROOT / "tests/ci-dependencies.json").read_text())
        pin, = [entry for entry in lock["charts"] if entry["chart"] == "metrics-server"]
        self.assertEqual(pin["version"], "3.14.0")
        self.assertEqual(pin["sha256"], "c2ca1185c01e6e7f53dd1b7d131f0c9b3fa50e003ed068b784563a1b5a3422a1")
        resources = yaml.safe_load((ROOT / "kustomization.yaml").read_text())["resources"]
        self.assertIn(str(APP.relative_to(ROOT)), resources)

    def test_a_misspelled_helm_selector_does_not_count_as_placement(self):
        repo = Repository()
        values = repo.apps["metrics-server"]["spec"]["source"]["helm"]["valuesObject"]
        values["nodeSelektor"] = values.pop("nodeSelector")
        docs = repo.render("metrics-server")
        pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
        self.assertNotEqual(pod.get("nodeSelector", {}).get("compute-class"), "standard")


if __name__ == "__main__":
    unittest.main()
