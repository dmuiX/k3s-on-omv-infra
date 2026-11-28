"""Offline unit and manifest tests for prometheus-backup-pipeline."""
from io import BytesIO
from pathlib import Path
import types
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

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

        _, mod = load_pipeline_module()
        cleanup_mounts = mod.build_cleanup_job("test")["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        self.assertEqual(cleanup_mounts, [{
            "name": "prometheus-data",
            "mountPath": "/prometheus",
            "subPath": "prometheus-db",
        }])

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

    def test_api_retries_transient_429_storage_reinitializing(self):
        _, mod = load_pipeline_module()
        body = (
            b'{"kind":"Status","status":"Failure","message":"storage is (re)initializing",'
            b'"reason":"TooManyRequests","details":{"retryAfterSeconds":2},"code":429}'
        )
        transient = HTTPError(
            "https://10.43.0.1:443/apis/k8up.io/v1/namespaces/kube-prometheus-stack/snapshots",
            429,
            "Too Many Requests",
            hdrs={},
            fp=BytesIO(body),
        )
        ok_resp = mock.MagicMock()
        ok_resp.__enter__.return_value = ok_resp
        ok_resp.read.return_value = b'{"items": []}'
        sleeps = []

        with (
            mock.patch.object(mod, "api_settings", return_value=("https://10.43.0.1:443", {}, None)),
            mock.patch.object(mod, "urlopen", side_effect=[transient, ok_resp]) as mocked_open,
            mock.patch.object(mod.time, "sleep", side_effect=sleeps.append),
        ):
            result = mod.api("/apis/k8up.io/v1/namespaces/kube-prometheus-stack/snapshots")

        self.assertEqual(result, {"items": []})
        self.assertEqual(mocked_open.call_count, 2)
        self.assertEqual(sleeps, [2.0])

    def test_api_does_not_retry_non_transient_403(self):
        _, mod = load_pipeline_module()
        forbidden = HTTPError(
            "https://10.43.0.1:443/apis/k8up.io/v1/namespaces/kube-prometheus-stack/snapshots",
            403,
            "Forbidden",
            hdrs={},
            fp=BytesIO(b'{"code":403}'),
        )
        sleeps = []

        with (
            mock.patch.object(mod, "api_settings", return_value=("https://10.43.0.1:443", {}, None)),
            mock.patch.object(mod, "urlopen", side_effect=[forbidden]) as mocked_open,
            mock.patch.object(mod.time, "sleep", side_effect=sleeps.append),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                mod.api("/apis/k8up.io/v1/namespaces/kube-prometheus-stack/snapshots")

        self.assertEqual(mocked_open.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_api_retries_408_5xx_retry_after_header_and_get_transport_errors(self):
        _, mod = load_pipeline_module()
        url = "https://10.43.0.1:443/apis/k8up.io/v1/namespaces/kube-prometheus-stack/snapshots"
        timeout_408 = HTTPError(url, 408, "Request Timeout", hdrs={}, fp=BytesIO(b""))
        unavailable_503 = HTTPError(
            url,
            503,
            "Service Unavailable",
            hdrs={"Retry-After": "3"},
            fp=BytesIO(b'{"code":503}'),
        )
        transport_err = URLError("temporary reset")
        ok_resp = mock.MagicMock()
        ok_resp.__enter__.return_value = ok_resp
        ok_resp.read.return_value = b'{"items": ["ok"]}'
        sleeps = []

        with (
            mock.patch.object(mod, "api_settings", return_value=("https://10.43.0.1:443", {}, None)),
            mock.patch.object(
                mod,
                "urlopen",
                side_effect=[timeout_408, unavailable_503, transport_err, ok_resp],
            ) as mocked_open,
            mock.patch.object(mod.time, "sleep", side_effect=sleeps.append),
        ):
            result = mod.api("/apis/k8up.io/v1/namespaces/kube-prometheus-stack/snapshots")

        self.assertEqual(result, {"items": ["ok"]})
        self.assertEqual(mocked_open.call_count, 4)
        self.assertEqual(sleeps, [1.0, 3.0, 4.0])

    def test_api_stops_after_max_attempts_on_persistent_503(self):
        _, mod = load_pipeline_module()
        url = "https://10.43.0.1:443/apis/k8up.io/v1/namespaces/kube-prometheus-stack/snapshots"
        errors = [
            HTTPError(url, 503, "Service Unavailable", hdrs={}, fp=BytesIO(b"unavailable"))
            for _ in range(mod.API_MAX_ATTEMPTS)
        ]
        sleeps = []

        with (
            mock.patch.object(mod, "api_settings", return_value=("https://10.43.0.1:443", {}, None)),
            mock.patch.object(mod, "urlopen", side_effect=errors) as mocked_open,
            mock.patch.object(mod.time, "sleep", side_effect=sleeps.append),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                mod.api("/apis/k8up.io/v1/namespaces/kube-prometheus-stack/snapshots")

        self.assertEqual(mocked_open.call_count, mod.API_MAX_ATTEMPTS)
        self.assertEqual(sleeps, [1.0, 2.0, 4.0, 8.0, 16.0])

    def test_api_does_not_retry_post_transport_error(self):
        _, mod = load_pipeline_module()
        sleeps = []

        with (
            mock.patch.object(mod, "api_settings", return_value=("https://10.43.0.1:443", {}, None)),
            mock.patch.object(mod, "urlopen", side_effect=URLError("reset after write")) as mocked_open,
            mock.patch.object(mod.time, "sleep", side_effect=sleeps.append),
        ):
            with self.assertRaisesRegex(RuntimeError, "POST"):
                mod.api("/apis/k8up.io/v1/namespaces/kube-prometheus-stack/backups", "POST", {"kind": "Backup"})

        self.assertEqual(mocked_open.call_count, 1)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
