"""Exercise the actual ConfigMap script, with only synthetic storage and API replies."""

from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import ModuleType
from typing import Any, Iterator
import unittest
from unittest.mock import patch

import yaml

MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "09-kube-prometheus-stack/resources/prometheus-snapshot.yaml"
)
UID = "f83f3122-9cf4-4e10-9177-28c658c27611"
OTHER_UID = "f83f3122-9cf4-4e10-9177-28c658c27612"
NAME = "20260102T030405Z-123456789abcdef0"
INTENT = {"version": 1, "backup_uid": UID, "stage": "intent"}
READY = INTENT | {"stage": "snapshot_ready", "snapshot_name": NAME}


class SnapshotBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config, self.pre_backup = yaml.safe_load_all(MANIFEST.read_text())
        self.tool = ModuleType("snapshot")
        script = self.config["data"]["snapshot.py"]
        exec(compile(script, str(MANIFEST), "exec"), vars(self.tool))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "prometheus-db" / "snapshots"
        self.root.mkdir(parents=True)
        self.pending = self.root / ".k8up-pending"
        self.snapshot = self.root / NAME
        self.output = io.BytesIO()

    def ready(self) -> dict[str, Any]:
        info = self.snapshot.stat()
        return READY | {"st_dev": info.st_dev, "st_ino": info.st_ino}

    def read_binding(self, uid: str = UID) -> tuple[str, tuple[int, int]]:
        with self.tool.directory_fd(self.root) as fd:
            binding: tuple[str, tuple[int, int]] = self.tool.read_binding(fd, uid)
        return binding

    def test_immutable_tool_reference_and_owner_uid(self) -> None:
        spec = self.pre_backup["spec"]
        pod = spec["pod"]["spec"]
        volume = next(v for v in pod["volumes"] if v["name"] == "snapshot-tool")
        metadata = self.config["metadata"]
        self.assertIs(self.config["immutable"], True)
        self.assertEqual(metadata["name"], "prometheus-snapshot-contract-v1")
        self.assertEqual(volume["configMap"]["name"], metadata["name"])
        self.assertEqual(metadata["annotations"]["argocd.argoproj.io/sync-wave"], "-1")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(
            spec["backupCommand"], "/usr/local/bin/python /tools/snapshot.py"
        )
        (env,) = pod["containers"][0]["env"]
        self.assertEqual(env["name"], "K8UP_BACKUP_UID")
        self.assertEqual(
            env["valueFrom"]["fieldRef"]["fieldPath"],
            "metadata.labels['k8up.io/ownerBackupUID']",
        )

    def test_producer_mounts_only_the_writable_snapshots_subpath(self) -> None:
        pod = self.pre_backup["spec"]["pod"]["spec"]
        self.assertNotIn("fsGroup", pod["securityContext"])
        self.assertEqual(pod["securityContext"]["runAsUser"], 1000)
        self.assertEqual(pod["securityContext"]["runAsGroup"], 2000)
        self.assertNotIn("initContainers", pod)
        self.assertEqual(
            pod["containers"][0]["volumeMounts"],
            [
                {
                    "name": "prometheus-data",
                    "mountPath": "/snapshots",
                    "subPath": "prometheus-db/snapshots",
                },
                {"name": "snapshot-tool", "mountPath": "/tools", "readOnly": True},
            ],
        )
        (pvc,) = [v for v in pod["volumes"] if "persistentVolumeClaim" in v]
        self.assertEqual(pvc, {
            "name": "prometheus-data",
            "persistentVolumeClaim": {
                "claimName": "prometheus-kube-prometheus-stack-prometheus-db-"
                "prometheus-kube-prometheus-stack-prometheus-0",
            },
        })

    @unittest.skipUnless(sys.platform == "linux", "Snapshot streaming uses Linux procfs")
    def test_intent_before_request_and_exact_binding_before_tar(self) -> None:
        def native_snapshot() -> str:
            self.assertEqual(json.loads(self.pending.read_bytes()), INTENT)
            self.snapshot.mkdir()
            (self.snapshot / "index").write_bytes(b"synthetic TSDB data")
            return NAME

        open_tar = tarfile.open

        def require_binding(*args: Any, **kwargs: Any) -> tarfile.TarFile:
            self.assertEqual(json.loads(self.pending.read_bytes()), self.ready())
            return open_tar(*args, **kwargs)

        with (
            patch.object(
                self.tool, "request_snapshot", side_effect=native_snapshot
            ) as request,
            patch.object(self.tool.tarfile, "open", side_effect=require_binding),
        ):
            self.tool.stream_snapshot(self.root, UID, self.output)
            request.assert_called_once_with()
        self.output.seek(0)
        with tarfile.open(fileobj=self.output, mode="r:") as archive:
            self.assertEqual(archive.getnames(), [NAME, NAME + "/index"])
        ready = self.ready()
        self.assertEqual(
            self.read_binding(), (NAME, (ready["st_dev"], ready["st_ino"]))
        )
        self.assertEqual(json.loads(self.pending.read_bytes()), ready)
        self.assertTrue((self.snapshot / "index").is_file())

    def test_read_binding_requires_exact_uid_and_valid_directory_identity(self) -> None:
        self.snapshot.mkdir()
        valid = self.ready()
        for uid, record in (
            (OTHER_UID, valid),
            (UID, valid | {"snapshot_name": "../" + NAME}),
            (UID, valid | {"st_dev": True}),
            (UID, valid | {"st_dev": -1}),
            (UID, valid | {"st_ino": 0}),
            (UID, valid | {"st_ino": "123"}),
            (UID, READY),
        ):
            with self.subTest(uid=uid, record=record):
                self.pending.write_text(json.dumps(record, sort_keys=True) + "\n")
                with self.assertRaises(ValueError):
                    self.read_binding(uid)
        self.assertTrue(self.snapshot.is_dir())

    def test_missing_uid_or_undurable_intent_never_calls_api(self) -> None:
        with patch.object(self.tool, "request_snapshot") as request:
            with self.assertRaises(ValueError):
                self.tool.stream_snapshot(self.root, "", self.output)
            self.assertFalse(self.pending.exists())
            with (
                patch.object(self.tool.os, "fsync", side_effect=OSError),
                self.assertRaises(OSError),
            ):
                self.tool.stream_snapshot(self.root, UID, self.output)
            request.assert_not_called()
        self.assertTrue(self.pending.exists())

    def test_missing_snapshots_mount_does_not_create_it_or_call_api(self) -> None:
        self.root.rmdir()
        with patch.object(self.tool, "request_snapshot") as request:
            with self.assertRaises(FileNotFoundError):
                self.tool.stream_snapshot(self.root, UID, self.output)
            request.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_uncertain_request_retains_intent_and_blocks_retry(self) -> None:
        def unknown_response() -> str:
            self.snapshot.mkdir()  # Prometheus may have processed the POST before timeout.
            raise TimeoutError

        with patch.object(
            self.tool, "request_snapshot", side_effect=unknown_response
        ) as request:
            with self.assertRaises(TimeoutError):
                self.tool.stream_snapshot(self.root, UID, self.output)
            with self.assertRaises(FileExistsError):
                self.tool.stream_snapshot(self.root, OTHER_UID, self.output)
            request.assert_called_once_with()
        self.assertEqual(json.loads(self.pending.read_bytes()), INTENT)
        with self.assertRaises(ValueError):
            self.read_binding()
        self.assertTrue(self.snapshot.is_dir())
        self.assertEqual(self.output.getvalue(), b"")

    def test_failed_promotion_cannot_expose_ready_binding(self) -> None:
        self.snapshot.mkdir(parents=True)
        with (
            patch.object(self.tool, "request_snapshot", return_value=NAME),
            patch.object(self.tool.os, "replace", side_effect=OSError),
            self.assertRaises(OSError),
        ):
            self.tool.stream_snapshot(self.root, UID, self.output)
        self.assertEqual(json.loads(self.pending.read_bytes()), INTENT)
        self.assertEqual(self.output.getvalue(), b"")

    def test_invalid_native_name_preserves_intent(self) -> None:
        with (
            patch.object(self.tool, "request_snapshot", return_value="../" + NAME),
            self.assertRaises(ValueError),
        ):
            self.tool.stream_snapshot(self.root, UID, self.output)
        self.assertEqual(json.loads(self.pending.read_bytes()), INTENT)

    def test_same_name_replacement_before_tar_does_not_change_binding(self) -> None:
        self.snapshot.mkdir()
        ready = self.ready()
        original = self.root / "displaced-original"
        replace = os.replace

        def promote_and_swap(source: str, target: str, **kwargs: int) -> None:
            replace(source, target, **kwargs)
            self.snapshot.rename(original)
            self.snapshot.mkdir()

        with (
            patch.object(self.tool, "request_snapshot", return_value=NAME),
            patch.object(self.tool.os, "replace", side_effect=promote_and_swap),
            self.assertRaisesRegex(ValueError, "replaced before streaming"),
        ):
            self.tool.stream_snapshot(self.root, UID, self.output)
        self.assertEqual(json.loads(self.pending.read_bytes()), ready)
        self.assertTrue(original.is_dir())
        self.assertTrue(self.snapshot.is_dir())
        self.assertEqual(self.output.getvalue(), b"")

    @unittest.skipUnless(sys.platform == "linux", "Snapshot streaming uses Linux procfs")
    def test_temporary_snapshot_replacement_during_tar_archives_only_original(self) -> None:
        self.snapshot.mkdir()
        (self.snapshot / "index").write_bytes(b"original TSDB data")
        original = self.root / "displaced-original"
        replacement = self.root / "replacement"
        replacement.mkdir()
        (replacement / "index").write_bytes(b"replacement data")
        open_tar = tarfile.open

        @contextmanager
        def swap_during_tar(*args: Any, **kwargs: Any) -> Iterator[tarfile.TarFile]:
            self.snapshot.rename(original)
            replacement.rename(self.snapshot)
            try:
                with open_tar(*args, **kwargs) as archive:
                    yield archive
            finally:
                # Restore the checked name before the producer's post-stream check.
                self.snapshot.rename(replacement)
                original.rename(self.snapshot)

        with (
            patch.object(self.tool, "request_snapshot", return_value=NAME),
            patch.object(self.tool.tarfile, "open", side_effect=swap_during_tar),
        ):
            self.tool.stream_snapshot(self.root, UID, self.output)

        self.output.seek(0)
        with tarfile.open(fileobj=self.output, mode="r:") as archive:
            member = archive.extractfile(NAME + "/index")
            assert member is not None
            with member:
                self.assertEqual(member.read(), b"original TSDB data")

    def test_legacy_partial_or_unknown_marker_blocks_producer_and_binding_read(
        self,
    ) -> None:
        self.snapshot.mkdir()
        ready = (json.dumps(self.ready(), sort_keys=True) + "\n").encode()
        for record in (
            b"",
            (NAME + "\n").encode(),
            ready[:-1],
            ready + b"extra",
            ready.replace(b'"version": 1', b'"version": 2'),
        ):
            with self.subTest(record=record):
                self.pending.write_bytes(record)
                with patch.object(self.tool, "request_snapshot") as request:
                    with self.assertRaises(FileExistsError):
                        self.tool.stream_snapshot(self.root, UID, self.output)
                    request.assert_not_called()
                with self.assertRaises(ValueError):
                    self.read_binding()
                self.assertEqual(self.pending.read_bytes(), record)
        self.assertTrue(self.snapshot.is_dir())

    def test_binding_rejects_missing_or_symlinked_marker(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.read_binding()
        self.pending.symlink_to(self.root / "missing")
        with self.assertRaises(OSError):
            self.read_binding()

    def test_symlinked_snapshot_does_not_promote_intent(self) -> None:
        self.snapshot.symlink_to(self.root, target_is_directory=True)
        with (
            patch.object(self.tool, "request_snapshot", return_value=NAME),
            self.assertRaises(OSError),
        ):
            self.tool.stream_snapshot(self.root, UID, self.output)
        self.assertEqual(json.loads(self.pending.read_bytes()), INTENT)
        self.assertEqual(self.output.getvalue(), b"")

    def test_http_redirect_does_not_trigger_second_request(self) -> None:
        with patch.object(self.tool, "HTTPConnection") as http:
            connection = http.return_value
            response = connection.getresponse.return_value.__enter__.return_value
            response.status = 302
            with self.assertRaises(ValueError):
                self.tool.request_snapshot()
            connection.request.assert_called_once_with("POST", self.tool.SNAPSHOT_API)
