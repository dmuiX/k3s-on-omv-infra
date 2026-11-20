"""K3s component contracts against the pinned chart and public Application sources."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from workload_contracts import Repository

STACK = "kube-prometheus-stack"
SCHEDULER_GROUP = "kube-scheduler.rules"
COMPONENTS = ("kube-controller-manager", "kube-scheduler", "kube-proxy")
RETIRED_GROUPS = {
    "kubernetes-system-controller-manager",
    "kubernetes-system-scheduler",
    "kubernetes-system-kube-proxy",
    "etcd",
}
CUSTOM_ALERTS = {
    "K3sControllerManagerLeaseNotRenewing",
    "K3sSchedulerLeaseNotRenewing",
    "K3sControllerManagerActivityMissing",
    "K3sProxySyncMetricMissing",
    "K3sProxyNodeInventoryMissing",
}


class K3sComponentMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Uses PLATFORM_CHART_AUDIT and synthetic site values, never Live or a cluster.
        cls.docs = Repository().render(STACK)
        cls.rule_docs = [d for d in cls.docs if d["kind"] == "PrometheusRule"]
        cls.groups = [g for d in cls.rule_docs for g in d["spec"]["groups"]]
        cls.rules = [r for g in cls.groups for r in g["rules"]]
        (cls.prometheus,) = (d for d in cls.docs if d["kind"] == "Prometheus")

    def test_nine_upstream_scheduler_quantiles_use_only_apiserver(self):
        self.assertEqual(
            self.prometheus["metadata"]["labels"]["chart"], f"{STACK}-92.10.1"
        )
        (group,) = (g for g in self.groups if g["name"] == SCHEDULER_GROUP)
        expected = []
        for family in (
            "scheduler_scheduling_attempt_duration_seconds",
            "scheduler_scheduling_algorithm_duration_seconds",
            "scheduler_pod_scheduling_sli_duration_seconds",
        ):
            record = f"cluster_quantile:{family}:histogram_quantile"
            for q in ("0.5", "0.9", "0.99"):
                expr = (
                    f"histogram_quantile({q}, sum(rate({family}_bucket"
                    '{job="apiserver"}[5m])) without(instance, pod))'
                )
                expected.append((record, q, expr))
        self.assertCountEqual(
            [(r["record"], r["labels"]["quantile"], r["expr"]) for r in group["rules"]],
            expected,
        )
        # With pruning off, a second group emitting the same series is not a handoff.
        records = {record for record, _, _ in expected}
        self.assertEqual(sum(r.get("record") in records for r in self.rules), 9)

    def test_dedicated_job_alert_groups_and_alerts_are_absent(self):
        self.assertFalse(RETIRED_GROUPS & {g["name"] for g in self.groups})
        retired_alerts = {
            component + suffix
            for component in ("KubeControllerManager", "KubeScheduler", "KubeProxy")
            for suffix in ("Down", "InstanceUnreachable")
        }
        self.assertFalse(retired_alerts & {r.get("alert") for r in self.rules})

    def test_dedicated_services_and_monitors_are_absent_but_shared_scrapes_remain(self):
        retired = {f"{STACK}-{component}" for component in COMPONENTS}
        for kind in ("Service", "ServiceMonitor"):
            with self.subTest(kind=kind):
                names = {d["metadata"]["name"] for d in self.docs if d["kind"] == kind}
                self.assertFalse(names & retired)
        monitors = {
            d["metadata"]["name"] for d in self.docs if d["kind"] == "ServiceMonitor"
        }
        for source in ("apiserver", "kubelet", "kube-state-metrics"):
            self.assertIn(f"{STACK}-{source}", monitors)

    def test_empty_etcd_scrape_and_dependent_resources_do_not_render(self):
        retired = {f"{STACK}-kube-etcd", f"{STACK}-etcd"}
        self.assertFalse(retired & {d["metadata"]["name"] for d in self.docs})

    def test_kubelet_main_endpoint_supplies_proxy_rule_labels(self):
        name = f"{STACK}-kubelet"
        (spec,) = (
            d["spec"] for d in self.docs
            if d["kind"] == "ServiceMonitor" and d["metadata"]["name"] == name
        )
        self.assertEqual(spec["namespaceSelector"]["matchNames"], ["kube-system"])
        self.assertEqual(spec["selector"]["matchLabels"][spec["jobLabel"]], "kubelet")
        (main,) = (e for e in spec["endpoints"] if e.get("path", "/metrics") == "/metrics")
        self.assertEqual(main["port"], "https-metrics")
        self.assertEqual(
            main["relabelings"],
            [{"sourceLabels": ["__metrics_path__"], "targetLabel": "metrics_path", "action": "replace"}],
        )
        # Sample filters may discard series, but do not strip or rewrite node.
        for rule in main.get("metricRelabelings", []):
            self.assertIn(rule["action"], ("drop", "keep"))
        # The operator discovers node labels from Node targetRefs, not pod labels
        # or an explicit node relabel in the monitor. Live values need verification.
        (args,) = (
            c["args"] for d in self.docs
            if d["kind"] == "Deployment" and d["metadata"]["name"] == f"{STACK}-operator"
            for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == STACK
        )
        self.assertIn(f"--kubelet-service=kube-system/{name}", args)
        self.assertIn("--kubelet-endpoints=true", args)

    def test_only_component_dashboards_are_hidden_from_the_sidecar(self):
        labels = {
            d["metadata"]["name"]: d["metadata"]["labels"]["grafana_dashboard"]
            for d in self.docs
            if d["kind"] == "ConfigMap"
            and "grafana_dashboard" in d["metadata"].get("labels", {})
        }
        self.assertEqual(labels[f"{STACK}-apiserver"], "1")
        self.assertEqual(
            {n for n, v in labels.items() if v != "1"},
            {f"{STACK}-{n}" for n in ("scheduler", "controller-manager", "proxy")},
        )
        (sidecar,) = (
            c
            for d in self.docs
            if d["kind"] == "Deployment"
            for c in d["spec"]["template"]["spec"]["containers"]
            if c["name"] == "grafana-sc-dashboard"
        )
        selector = {e["name"]: e.get("value") for e in sidecar["env"]}
        self.assertEqual(
            (selector["LABEL"], selector["LABEL_VALUE"]), ("grafana_dashboard", "1")
        )

    def test_promtool_on_actual_rendered_rules(self):
        groups = [
            g for g in self.groups if g["name"] in {SCHEDULER_GROUP, "k3s.components"}
        ]
        self.assertEqual(len(groups), 2)
        fixture = (
            Path(__file__).with_name("fixtures") / "k3s-component-metrics.test.yml"
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "rules.yml").write_text(yaml.safe_dump({"groups": groups}))
            shutil.copyfile(fixture, Path(tmp) / fixture.name)
            subprocess.run(
                ["promtool", "test", "rules", fixture.name], cwd=tmp, check=True
            )

    def test_unrelated_rule_groups_match_the_pinned_chart(self):
        # Compare to the chart's own groups, not a second copy of its rules in a fixture.
        baseline = Repository()
        spec = baseline.apps[STACK]["spec"]
        (chart,) = (
            source for source in spec["sources"] if source.get("chart") == STACK
        )
        spec["sources"] = [chart]
        chart["helm"] = {"skipCrds": True, "valuesObject": {"crds": {"enabled": False}}}
        unchanged = [
            g
            for d in baseline.render(STACK)
            if d["kind"] == "PrometheusRule"
            for g in d["spec"]["groups"]
            if g["name"] not in RETIRED_GROUPS | {SCHEDULER_GROUP}
        ]
        self.assertIn("kube-state-metrics", {g["name"] for g in unchanged})
        for expected in unchanged:
            with self.subTest(group=expected["name"]):
                (actual,) = (g for g in self.groups if g["name"] == expected["name"])
                self.assertEqual(actual, expected)
        self.assertEqual(sum(g["name"] == "platform.required" for g in self.groups), 1)

    def test_replacement_alerts_are_present_once_and_selected_by_prometheus(self):
        (custom,) = (
            d for d in self.rule_docs if d["metadata"]["name"] == "k3s-component-rules"
        )
        (group,) = custom["spec"]["groups"]
        self.assertCountEqual([r["alert"] for r in group["rules"]], CUSTOM_ALERTS)
        self.assertCountEqual(
            [r["alert"] for r in self.rules if r.get("alert") in CUSTOM_ALERTS],
            CUSTOM_ALERTS,
        )
        self.assertEqual(
            custom["metadata"]["namespace"], self.prometheus["metadata"]["namespace"]
        )
        self.assertEqual(self.prometheus["spec"]["ruleNamespaceSelector"], {})
        self.assertEqual(self.prometheus["spec"]["ruleSelector"], {})


if __name__ == "__main__":
    unittest.main()
