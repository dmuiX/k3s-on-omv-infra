"""Offline unit and manifest tests for prometheus-backup-pipeline."""
from pathlib import Path
import types
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "09-kube-prometheus-stack/resources/prometheus-backup-pipeline.yaml"


def load_pipeline_module():
    docs = list(yaml.safe_load_all(MANIFEST.read_text()))
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    module = types.ModuleType("prometheus_backup_pipeline")
    exec(compile(cm["data"]["pipeline.py"], str(MANIFEST), "exec"), module.__dict__)
    return docs, module


class PrometheusBackupPipelineTest(unittest.TestCase):
    def test_manifest_rbac_and_cronjob_contracts(self):
        docs, _ = load_pipeline_module()
        by_kind = {d["kind"]: d for d in docs}
        self.assertEqual(set(by_kind), {"ServiceAccount", "Role", "RoleBinding", "ConfigMap", "CronJob"})
        for doc in docs:
            self.assertEqual(doc["metadata"]["namespace"], "kube-prometheus-stack")

        role = by_kind["Role"]
        for rule in role["rules"]:
            self.assertNotIn("delete", rule["verbs"])
            self.assertNotIn("secrets", rule["resources"])

        cron = by_kind["CronJob"]["spec"]
        self.assertIs(cron["suspend"], False)
        self.assertEqual(cron["timeZone"], "Europe/Berlin")
        self.assertEqual(cron["concurrencyPolicy"], "Forbid")
        pod = cron["jobTemplate"]["spec"]["template"]
        self.assertEqual(pod["metadata"]["labels"]["app.kubernetes.io/name"], "prometheus-backup-pipeline")
        self.assertEqual(pod["metadata"]["labels"]["istio.io/dataplane-mode"], "none")
        container = pod["spec"]["containers"][0]
        self.assertRegex(container["image"], r"@sha256:[0-9a-f]{64}$")
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])

    def test_pipeline_runs_backup_check_cleanup_prune_in_order(self):
        _, mod = load_pipeline_module()
        calls = []
        snapshots = [{"items": []}, {"items": [{"spec": {"repository": "/repository", "id": "snap-1"}}]}]

        def fake_api(path, method="GET", body=None, **_kwargs):
            calls.append((method, path))
            if method == "GET" and path.endswith("/snapshots"):
                return snapshots.pop(0)
            if method == "GET" and "/jobs/" in path:
                return {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
            if method == "GET":
                return {"status": {"conditions": [{"type": "Completed", "status": "True", "reason": "Succeeded"}]}}
            return {}

        with mock.patch.object(mod, "api", side_effect=fake_api), mock.patch.object(mod.time, "sleep"):
            mod.run("20261126-030000")

        posts = [path for method, path in calls if method == "POST"]
        self.assertEqual(posts, [
            "/apis/k8up.io/v1/namespaces/kube-prometheus-stack/backups",
            "/apis/k8up.io/v1/namespaces/kube-prometheus-stack/checks",
            "/apis/batch/v1/namespaces/kube-prometheus-stack/jobs",
            "/apis/k8up.io/v1/namespaces/kube-prometheus-stack/prunes",
        ])

    def test_pipeline_fails_closed_before_cleanup_when_check_fails(self):
        _, mod = load_pipeline_module()
        posts = []
        snapshots = [{"items": []}, {"items": [{"spec": {"repository": "/repository", "id": "snap-1"}}]}]

        def fake_api(path, method="GET", body=None, **_kwargs):
            if method == "POST":
                posts.append(path)
                return {}
            if path.endswith("/snapshots"):
                return snapshots.pop(0)
            if "/checks/" in path:
                return {"status": {"conditions": [{"type": "Completed", "status": "True", "reason": "Failed"}]}}
            return {"status": {"conditions": [{"type": "Completed", "status": "True", "reason": "Succeeded"}]}}

        with mock.patch.object(mod, "api", side_effect=fake_api), mock.patch.object(mod.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "checks/prometheus-tsdb-check-test"):
                mod.run("test")

        self.assertEqual(posts, [
            "/apis/k8up.io/v1/namespaces/kube-prometheus-stack/backups",
            "/apis/k8up.io/v1/namespaces/kube-prometheus-stack/checks",
        ])

    def test_pipeline_fails_closed_when_no_new_snapshot_appears(self):
        _, mod = load_pipeline_module()
        posts = []

        def fake_api(path, method="GET", body=None, **_kwargs):
            if method == "POST":
                posts.append(path)
                return {}
            if path.endswith("/snapshots"):
                return {"items": [{"spec": {"repository": "/repository", "id": "old-snap"}}]}
            return {"status": {"conditions": [{"type": "Completed", "status": "True", "reason": "Succeeded"}]}}

        with mock.patch.object(mod, "api", side_effect=fake_api), mock.patch.object(mod.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "no new /repository Snapshot"):
                mod.run("test")

        self.assertEqual(posts, ["/apis/k8up.io/v1/namespaces/kube-prometheus-stack/backups"])


if __name__ == "__main__":
    unittest.main()
