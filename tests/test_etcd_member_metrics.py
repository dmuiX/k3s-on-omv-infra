"""Exercise the pinned exporter chart and the actual member alert expressions offline."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from workload_contracts import Repository


class EtcdMemberMetrics(unittest.TestCase):
    def test_only_a_read_only_textfile_input_is_added_to_the_exporter(self):
        app = "kube-prometheus-stack"
        repo = Repository()
        docs = repo.render(app)
        baseline = Repository()
        exporter = baseline.apps[app]["spec"]["sources"][0]["helm"]["valuesObject"][
            "prometheus-node-exporter"
        ]
        del exporter["extraArgs"], exporter["extraHostVolumeMounts"]

        def exporter_pod(resources):
            (daemon,) = [
                r
                for r in resources
                if r["kind"] == "DaemonSet"
                and r["metadata"]["namespace"] == "node-exporter"
            ]
            return daemon["spec"]["template"]["spec"]

        pod = exporter_pod(docs)
        (container,) = pod["containers"]
        (mount,) = [
            m for m in container["volumeMounts"] if m["name"] == "etcd-member-metrics"
        ]
        self.assertEqual(
            mount,
            {
                "name": "etcd-member-metrics",
                "mountPath": "/run/etcd-member-metrics",
                "readOnly": True,
            },
        )
        (volume,) = [v for v in pod["volumes"] if v["name"] == mount["name"]]
        self.assertEqual(
            volume["hostPath"],
            {"path": "/var/lib/etcd-member-metrics", "type": "DirectoryOrCreate"},
        )
        container["args"].remove("--collector.textfile.directory=" + mount["mountPath"])
        container["volumeMounts"].remove(mount)
        pod["volumes"].remove(volume)
        self.assertEqual(
            pod, exporter_pod(baseline.render(app))
        )  # including filesystem exclusions, ports and security

        (service,) = [
            r
            for r in docs
            if r["kind"] == "Service" and r["metadata"]["namespace"] == "node-exporter"
        ]
        (monitor,) = [
            r
            for r in docs
            if r["kind"] == "ServiceMonitor"
            and r["metadata"]["namespace"] == "node-exporter"
        ]
        labels = service["metadata"]["labels"]
        self.assertTrue(
            monitor["spec"]["selector"]["matchLabels"].items() <= labels.items()
        )
        self.assertEqual(labels[monitor["spec"]["jobLabel"]], "node-exporter")
        (endpoint,) = monitor["spec"]["endpoints"]
        self.assertIn(endpoint["port"], [p["name"] for p in service["spec"]["ports"]])

    def test_promtool_member_scenarios(self):
        root = Path(__file__).resolve().parents[1]
        spec = yaml.safe_load(
            (
                root / "09-kube-prometheus-stack/resources/etcd-member-alerts.yaml"
            ).read_text()
        )["spec"]
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "etcd-member.rules.yml").write_text(yaml.safe_dump(spec))
            fixture = shutil.copy(
                root / "tests/fixtures/etcd-member-alerts.test.yml", directory
            )
            subprocess.run(
                ["promtool", "test", "rules", fixture],
                cwd=directory,
                check=True,
                timeout=30,
            )
