"""Exercise the backup pipeline with a fake Kubernetes API and real marker files."""

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any, cast
import unittest
from unittest.mock import Mock, call, patch

import yaml

BASE = Path(__file__).resolve().parents[1] / "09-kube-prometheus-stack"
sys.path.insert(0, str(BASE.parent / "adapters/k8up-pipeline"))
import k8up_pipeline as shared

SPEC = spec_from_file_location("pipeline", BASE / "restore-test/pipeline.py")
assert SPEC is not None and SPEC.loader is not None
pipeline = module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline)

POD_UID = "e015a64d-fd10-43ac-a42b-3d293a567000"
BACKUP_UID = "e015a64d-fd10-43ac-a42b-3d293a567001"
CHECK_UID = "e015a64d-fd10-43ac-a42b-3d293a567002"
JOB_UID = "e015a64d-fd10-43ac-a42b-3d293a567003"
OTHER_UID = "e015a64d-fd10-43ac-a42b-3d293a567004"
NATIVE = "20260102T030405Z-123456789abcdef0"
OTHER_NATIVE = "20260102T030405Z-123456789abcdef1"
BACKUP = f"pt-{POD_UID}"
CHECK = f"prometheus-check-{POD_UID}"
JOB = f"prometheus-verify-{POD_UID}"
COMPLETED = {"type": "Completed", "status": "True", "reason": "Succeeded"}
STOPPED = {"type": "PreBackupPodReady", "status": "True", "reason": "Finished"}
VERIFIED = {"type": "Complete", "status": "True"}
FAILED = {"type": "Failed", "status": "True"}


def observed(name: str, uid: str, *conditions: dict[str, str]) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": "kube-prometheus-stack", "uid": uid},
        "status": {"finished": True, "conditions": list(conditions)},
    }


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lock = self.root / ".k8up-pipeline-lock"
        self.marker = self.root / ".k8up-pending"
        self.proof = self.root / f".k8up-verified-{POD_UID}.json"
        self.staging = self.root / f".k8up-cleanup-{POD_UID}"
        self.claimed = self.staging / NATIVE
        self.native = self.root / NATIVE
        self.native.mkdir()
        (self.native / "index").write_bytes(b"synthetic index")
        self.unrelated = self.root / OTHER_NATIVE
        self.unrelated.mkdir()
        self.producer = ModuleType("snapshot")
        manifest = BASE / "resources/prometheus-snapshot.yaml"
        config, _ = yaml.safe_load_all(manifest.read_text())
        exec(
            compile(config["data"]["snapshot.py"], str(manifest), "exec"),
            vars(self.producer),
        )
        self.api = Mock(spec=pipeline.KubeApi)
        self.api.request.side_effect = self.request
        self.batch = "/apis/batch/v1/namespaces/kube-prometheus-stack"
        self.k8up = "/apis/k8up.io/v1/namespaces/kube-prometheus-stack"
        self.backup_status = f"{self.k8up}/backups/{BACKUP}"
        self.check_status = f"{self.k8up}/checks/{CHECK}"
        self.job_status = f"{self.batch}/jobs/{JOB}"
        self.deployment = "/apis/apps/v1/namespaces/kube-prometheus-stack/deployments/prometheus-tsdb-snapshot"
        # Use the real Prometheus template patch and sentinel fields from the
        # effective base; cloning must preserve everything except the two inputs.
        override = yaml.safe_load((BASE / "restore-test/prometheus.patch").read_text())
        job = override["spec"]["jobTemplate"]["spec"]
        job.update(
            activeDeadlineSeconds=7200, ttlSecondsAfterFinished=3600, backoffLimit=0
        )
        job["template"]["metadata"] = {
            "labels": {"app": "verifier"},
            "annotations": {"key": "value"},
        }
        job["template"]["spec"]["automountServiceAccountToken"] = False
        self.template: dict[str, Any] = {
            "spec": {"jobTemplate": {"spec": job}, "suspend": True}
        }
        self.replies: dict[str, dict[str, Any] | Exception] = {
            f"{self.k8up}/backups": observed(BACKUP, BACKUP_UID),
            self.backup_status: observed(BACKUP, BACKUP_UID, COMPLETED, STOPPED),
            f"{self.k8up}/checks": observed(CHECK, CHECK_UID),
            self.check_status: observed(CHECK, CHECK_UID, COMPLETED),
            f"{self.batch}/cronjobs/prometheus-restore-test": self.template,
            f"{self.batch}/jobs": observed(JOB, JOB_UID),
            self.job_status: observed(JOB, JOB_UID, VERIFIED),
            self.deployment: pipeline.ApiError("GET", self.deployment, 404),
        }
        self.dev, self.ino = self.directory_identity(self.native)
        self.binding = self.producer.marker_bytes(
            BACKUP_UID, (NATIVE, (self.dev, self.ino))
        )

    @staticmethod
    def directory_identity(path: Path) -> tuple[int, int]:
        info = path.stat()
        return info.st_dev, info.st_ino

    def request(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if path == f"{self.k8up}/backups":
            self.assertEqual(
                json.loads(self.lock.read_bytes()), {"version": 1, "pod_uid": POD_UID}
            )
            self.assertFalse(self.marker.exists())
        reply = self.replies[path]
        if isinstance(reply, Exception):
            raise reply
        if path == self.backup_status and not self.marker.exists():
            self.marker.write_bytes(self.binding)
        return reply

    def run_pipeline(self) -> Path:
        return cast(Path, pipeline.run(self.api, self.producer, self.root, POD_UID))

    def assert_retained(self) -> None:
        self.assertTrue(self.lock.is_file())
        self.assertTrue((self.native / "index").is_file())
        self.assertTrue(self.unrelated.is_dir())
        self.assertFalse(self.proof.exists())
        self.assertFalse(self.staging.exists())

    def test_exact_protocol_and_durable_proof_before_marker_last_cleanup(self) -> None:
        template_before = deepcopy(self.template)
        sync_fd = os.fsync
        write_durable = self.producer.write_durable
        rename = os.rename
        remove = pipeline.remove_children
        root_info = self.root.stat()
        events = []
        paths = self.proof, self.native, self.claimed, self.marker, self.lock

        def sync(fd: int) -> None:
            sync_fd(fd)
            if os.path.samestat(os.fstat(fd), root_info):
                events.append(tuple(item.exists() for item in paths))

        def claim(
            source: str, target: str, *, src_dir_fd: int, dst_dir_fd: int
        ) -> None:
            self.assertEqual((source, target), (NATIVE, NATIVE))
            self.assertEqual(events[-1], (True, True, False, True, True))
            self.assertTrue(os.path.samestat(os.fstat(src_dir_fd), root_info))
            self.assertTrue(os.path.samestat(os.fstat(dst_dir_fd), self.staging.stat()))
            rename(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        def remove_verified(producer: ModuleType, fd: int, dev: int) -> None:
            self.assertEqual(stat.S_IMODE(self.staging.stat().st_mode), 0o700)
            self.assertTrue(os.path.samestat(os.fstat(fd), self.claimed.stat()))
            self.assertEqual(producer.directory_identity(os.fstat(fd)), (dev, self.ino))
            self.assertEqual(self.marker.read_bytes(), self.binding)
            self.assertEqual(events[-1], (True, False, True, True, True))
            remove(producer, fd, dev)

        with (
            patch.object(os, "fsync", side_effect=sync),
            patch.object(os, "rename", side_effect=claim) as stage,
            patch.object(
                self.producer, "write_durable", wraps=write_durable
            ) as durable,
            patch.object(
                pipeline, "remove_children", side_effect=remove_verified
            ) as cleanup,
        ):
            self.assertEqual(self.run_pipeline(), self.proof)
        stage.assert_called_once()
        cleanup.assert_called_once()
        self.assertEqual(durable.call_count, 2)
        self.assertEqual(
            events,
            [
                # Lock, proof, claim, deletion, and pending marker removal, in order.
                (False, True, False, False, True),
                (True, True, False, True, True),
                (True, False, True, True, True),
                (True, False, False, True, True),
                (True, False, False, False, True),
            ],
        )
        self.assertFalse(self.staging.exists())
        self.assertFalse(self.lock.exists())
        self.assertTrue(self.unrelated.is_dir())
        self.assertEqual(
            json.loads(self.proof.read_bytes()),
            {
                "version": 1,
                "pod_uid": POD_UID,
                "backup_uid": BACKUP_UID,
                "check_uid": CHECK_UID,
                "verifier_uid": JOB_UID,
                "run_tag": f"backup-run-{POD_UID}",
                "snapshot_name": NATIVE,
                "st_dev": self.dev,
                "st_ino": self.ino,
                "staging_directory": self.staging.name,
            },
        )
        self.assertEqual(self.template, template_before)
        expected_job = template_before["spec"]["jobTemplate"]["spec"]
        expected_job["template"]["metadata"]["annotations"].update(
            {
                "platform.k3s-on-omv.io/backup-run-tag": f"backup-run-{POD_UID}",
                "platform.k3s-on-omv.io/native-snapshot": NATIVE,
            }
        )
        k8up_spec = {
            "backend": {
                "repoPasswordSecretRef": {
                    "name": "prometheus-k8up-repository",
                    "key": "password",
                },
                "local": {"mountPath": "/repository"},
                "volumeMounts": [{"name": "repository", "mountPath": "/repository"}],
            },
            "podConfigRef": {"name": "prometheus-k8up-omv"},
            "activeDeadlineSeconds": 7200,
            "successfulJobsHistoryLimit": 3,
            "failedJobsHistoryLimit": 3,
        }
        self.assertEqual(
            self.api.request.call_args_list,
            [
                call(
                    f"{self.k8up}/backups",
                    {
                        "apiVersion": "k8up.io/v1",
                        "kind": "Backup",
                        "metadata": {
                            "namespace": "kube-prometheus-stack",
                            "name": BACKUP,
                        },
                        "spec": k8up_spec
                        | {
                            "labelSelectors": [
                                {
                                    "matchLabels": {
                                        "platform.k3s-on-omv.io/backup-set": "prometheus-tsdb"
                                    }
                                }
                            ],
                            "tags": [
                                "prometheus-tsdb",
                                "prometheus-replica-0",
                                f"backup-run-{POD_UID}",
                            ],
                        },
                    },
                ),
                call(self.backup_status),
                call(self.deployment),
                call(
                    f"{self.k8up}/checks",
                    {
                        "apiVersion": "k8up.io/v1",
                        "kind": "Check",
                        "metadata": {
                            "namespace": "kube-prometheus-stack",
                            "name": CHECK,
                        },
                        "spec": k8up_spec,
                    },
                ),
                call(self.check_status),
                call(f"{self.batch}/cronjobs/prometheus-restore-test"),
                call(
                    f"{self.batch}/jobs",
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "metadata": {"namespace": "kube-prometheus-stack", "name": JOB},
                        "spec": expected_job,
                    },
                ),
                call(self.job_status),
            ],
        )

    def test_native_k8up_prebackup_job_name_fits_dns_label(self) -> None:
        self.run_pipeline()
        _, backup = self.api.request.call_args_list[0].args
        job_name = f"backup-{backup['metadata']['name']}-prebackup"
        self.assertEqual(job_name, f"backup-pt-{POD_UID}-prebackup")
        self.assertLessEqual(len(job_name.encode("ascii")), 63)
        self.assertRegex(job_name, r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")

    def test_backup_create_collision_keeps_lock_and_blocks_next_pod(self) -> None:
        path = f"{self.k8up}/backups"
        self.replies[path] = pipeline.ApiError("POST", path, 409)
        with self.assertRaisesRegex(pipeline.PipelineError, "HTTP 409"):
            self.run_pipeline()
        self.assertEqual(self.api.request.call_count, 1)
        self.assert_retained()
        with self.assertRaisesRegex(pipeline.PipelineError, "previous pipeline run"):
            pipeline.run(self.api, self.producer, self.root, OTHER_UID)
        self.assertEqual(self.api.request.call_count, 1)
        self.assertFalse(self.marker.exists())

    def test_previous_marker_even_a_dangling_symlink_blocks_new_backup(self) -> None:
        self.marker.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(
            pipeline.PipelineError, "previous producer binding"
        ):
            self.run_pipeline()
        self.api.request.assert_not_called()
        self.assertTrue(self.marker.is_symlink())
        self.assert_retained()

    def test_undurable_lock_prevents_even_first_create(self) -> None:
        with (
            patch.object(os, "fsync", side_effect=OSError),
            self.assertRaises(OSError),
        ):
            self.run_pipeline()
        self.api.request.assert_not_called()
        self.assert_retained()

    def test_producer_still_present_times_out_even_if_completed_and_finished(
        self,
    ) -> None:
        self.replies[self.deployment] = {"metadata": {"deletionTimestamp": "now"}}
        with (
            patch.object(shared, "WAIT_LIMIT", 10),
            patch.object(shared.time, "monotonic", side_effect=[0, 1, 2, 10]),
            patch.object(shared.time, "sleep") as sleep,
            self.assertRaisesRegex(
                pipeline.PipelineError, f"timed out waiting for {BACKUP}"
            ),
        ):
            self.run_pipeline()
        sleep.assert_called_once_with(5)
        self.assertEqual(
            self.api.request.call_args_list[1:],
            [call(self.backup_status), call(self.deployment)],
        )
        self.assertEqual(self.marker.read_bytes(), self.binding)
        self.assert_retained()

    def test_backup_without_terminal_producer_condition_cannot_pass(self) -> None:
        self.replies[self.backup_status] = observed(BACKUP, BACKUP_UID, COMPLETED)
        with (
            patch.object(shared, "WAIT_LIMIT", 10),
            patch.object(shared.time, "monotonic", side_effect=[0, 1, 10]),
            self.assertRaisesRegex(pipeline.PipelineError, "timed out"),
        ):
            self.run_pipeline()
        self.assertEqual(self.marker.read_bytes(), self.binding)
        self.assert_retained()

    def test_wrong_uid_on_wait_for_any_created_object_stops_pipeline(self) -> None:
        for path, name, terminal in (
            (self.backup_status, BACKUP, (COMPLETED, STOPPED)),
            (self.check_status, CHECK, (COMPLETED,)),
            (self.job_status, JOB, (VERIFIED,)),
        ):
            with (
                self.subTest(name=name),
                patch.dict(self.replies, {path: observed(name, OTHER_UID, *terminal)}),
            ):
                with self.assertRaisesRegex(pipeline.PipelineError, "object replaced"):
                    self.run_pipeline()
                self.assertEqual(self.api.request.call_args, call(path))
                self.assertEqual(self.marker.read_bytes(), self.binding)
                self.assert_retained()
                self.lock.unlink()
                self.marker.unlink()

    def test_explicit_denial_does_not_create_verifier_or_cleanup(self) -> None:
        path = f"{self.k8up}/checks"
        self.replies[path] = pipeline.ApiError("POST", path, 403)
        with self.assertRaisesRegex(pipeline.PipelineError, "HTTP 403"):
            self.run_pipeline()
        self.assertEqual(self.api.request.call_args.args[0], path)
        self.assertEqual(self.marker.read_bytes(), self.binding)
        self.assert_retained()

    def test_unverified_conditions_do_not_authorize_cleanup(self) -> None:
        for path, result, diagnostic in (
            (
                self.backup_status,
                observed(BACKUP, BACKUP_UID, COMPLETED | {"reason": "Failed"}),
                "K8up operation",
            ),
            (
                self.check_status,
                observed(CHECK, CHECK_UID, COMPLETED | {"reason": "Failed"}),
                "K8up operation",
            ),
            (
                self.job_status,
                observed(JOB, JOB_UID, VERIFIED, FAILED),
                "verifier Job failed",
            ),
        ):
            with self.subTest(path=path), patch.dict(self.replies, {path: result}):
                with self.assertRaisesRegex(pipeline.PipelineError, diagnostic):
                    self.run_pipeline()
                self.assertEqual(self.api.request.call_args, call(path))
                self.assertEqual(self.marker.read_bytes(), self.binding)
                self.assert_retained()
                self.lock.unlink()
                self.marker.unlink()

    def test_binding_rejects_intent_wrong_uid_and_noncanonical_marker(self) -> None:
        for record in (
            self.producer.marker_bytes(BACKUP_UID),
            self.producer.marker_bytes(OTHER_UID, (NATIVE, (self.dev, self.ino))),
            self.binding + b" ",
        ):
            with self.subTest(record=record):
                self.binding = record
                with self.assertRaisesRegex(
                    pipeline.PipelineError, "exact ready binding"
                ):
                    self.run_pipeline()
                self.assertEqual(self.api.request.call_args, call(self.deployment))
                self.assertEqual(self.marker.read_bytes(), record)
                self.assert_retained()
                self.lock.unlink()
                self.marker.unlink()

    def test_binding_rechecked_after_attestation(self) -> None:
        def replace_marker(
            path: str, body: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            if path == self.job_status:
                # Same Backup UID and name, but a different native directory identity.
                self.binding = self.producer.marker_bytes(
                    BACKUP_UID, (NATIVE, self.directory_identity(self.unrelated))
                )
                self.marker.write_bytes(self.binding)
            return self.request(path, body)

        self.api.request.side_effect = replace_marker
        with self.assertRaisesRegex(pipeline.PipelineError, "final producer binding"):
            self.run_pipeline()
        self.assertEqual(self.marker.read_bytes(), self.binding)
        self.assert_retained()

    def test_verifier_must_be_suspended_and_consume_exact_inputs(self) -> None:
        self.template["spec"]["suspend"] = False
        with self.assertRaisesRegex(pipeline.PipelineError, "must be suspended"):
            self.run_pipeline()
        self.assert_retained()
        self.lock.unlink()
        self.marker.unlink()
        self.template["spec"]["suspend"] = True
        pod = self.template["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        del pod["initContainers"][0]["env"]
        with self.assertRaisesRegex(pipeline.PipelineError, "exact run inputs"):
            self.run_pipeline()
        self.assert_retained()
        self.assertEqual(
            self.api.request.call_args,
            call(f"{self.batch}/cronjobs/prometheus-restore-test"),
        )

    def test_proof_directory_fsync_failure_prevents_claim_and_deletion(self) -> None:
        sync_fd = os.fsync
        root_info = self.root.stat()

        def fail_sync(fd: int) -> None:
            if self.proof.exists() and os.path.samestat(os.fstat(fd), root_info):
                raise OSError
            sync_fd(fd)

        with (
            patch.object(os, "fsync", side_effect=fail_sync),
            patch.object(os, "rename") as claim,
            patch.object(pipeline, "remove_children") as cleanup,
            self.assertRaises(OSError),
        ):
            self.run_pipeline()
        claim.assert_not_called()
        cleanup.assert_not_called()
        self.assertEqual(self.marker.read_bytes(), self.binding)
        self.assertTrue(self.lock.exists())
        self.assertTrue(self.native.exists())
        self.assertFalse(self.staging.exists())

    def test_existing_proof_is_not_overwritten_or_used_to_authorize_cleanup(
        self,
    ) -> None:
        self.proof.write_bytes(b"prior evidence")
        with (
            patch.object(os, "rename") as claim,
            patch.object(pipeline, "remove_children") as cleanup,
            self.assertRaises(FileExistsError),
        ):
            self.run_pipeline()
        claim.assert_not_called()
        cleanup.assert_not_called()
        self.assertEqual(self.proof.read_bytes(), b"prior evidence")
        self.assertEqual(self.marker.read_bytes(), self.binding)
        self.assertTrue(self.lock.exists())
        self.assertTrue(self.native.exists())
        self.assertFalse(self.staging.exists())

    def test_existing_staging_is_not_reused(self) -> None:
        self.claimed.mkdir(parents=True)
        sentinel = self.claimed / "prior-claim"
        sentinel.write_bytes(b"keep")
        with (
            patch.object(os, "rename") as claim,
            patch.object(pipeline, "remove_children") as cleanup,
            self.assertRaises(FileExistsError),
        ):
            self.run_pipeline()
        claim.assert_not_called()
        cleanup.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertTrue(self.native.is_dir())
        self.assert_verified_run_blocked()

    def assert_verified_run_blocked(self) -> None:
        self.assertTrue(self.lock.exists())
        self.assertEqual(self.marker.read_bytes(), self.binding)
        self.assertTrue(self.proof.is_file())
        count = self.api.request.call_count
        with self.assertRaisesRegex(pipeline.PipelineError, "previous pipeline run"):
            pipeline.run(self.api, self.producer, self.root, OTHER_UID)
        self.assertEqual(self.api.request.call_count, count)

    def test_same_name_replacement_before_claim_is_never_removed(self) -> None:
        original = self.root / "displaced-original"
        rename = os.rename

        def replace_then_claim(source: str, target: str, **kwargs: int) -> None:
            rename(self.native, original)
            self.native.mkdir()
            (self.native / "replacement").write_bytes(b"keep")
            rename(source, target, **kwargs)

        with (
            patch.object(os, "rename", side_effect=replace_then_claim),
            patch.object(pipeline, "remove_children") as cleanup,
            self.assertRaisesRegex(pipeline.PipelineError, "identity does not match"),
        ):
            self.run_pipeline()
        cleanup.assert_not_called()
        self.assertEqual((self.claimed / "replacement").read_bytes(), b"keep")
        self.assertEqual((original / "index").read_bytes(), b"synthetic index")
        self.assert_verified_run_blocked()

    def test_recheck_blocks_same_name_replacement_inside_staging(self) -> None:
        original = self.staging / "displaced-original"
        check_identity = self.producer.directory_identity
        swapped = False

        def swap_after_identification(info: os.stat_result) -> tuple[int, int]:
            nonlocal swapped
            identity: tuple[int, int] = check_identity(info)
            if (
                identity == (self.dev, self.ino)
                and self.claimed.is_dir()
                and not swapped
            ):
                self.claimed.rename(original)
                self.claimed.mkdir()
                (self.claimed / "replacement").write_bytes(b"keep")
                swapped = True
            return identity

        with (
            patch.object(
                self.producer,
                "directory_identity",
                side_effect=swap_after_identification,
            ),
            patch.object(pipeline, "remove_children") as cleanup,
            self.assertRaisesRegex(pipeline.PipelineError, "changed before deletion"),
        ):
            self.run_pipeline()
        self.assertTrue(swapped)
        cleanup.assert_not_called()
        self.assertEqual((self.claimed / "replacement").read_bytes(), b"keep")
        self.assertEqual((original / "index").read_bytes(), b"synthetic index")
        self.assert_verified_run_blocked()

    def test_cleanup_traverses_the_checked_fd_after_same_name_swap(self) -> None:
        original = self.staging / "displaced-original"
        remove = pipeline.remove_children

        def swap_after_open(producer: ModuleType, fd: int, device: int) -> None:
            self.assertEqual(producer.directory_identity(os.fstat(fd)), (self.dev, self.ino))
            self.claimed.rename(original)
            self.claimed.mkdir()
            (self.claimed / "replacement").write_bytes(b"keep")
            remove(producer, fd, device)

        with (
            patch.object(pipeline, "remove_children", side_effect=swap_after_open),
            self.assertRaisesRegex(pipeline.PipelineError, "changed before removal"),
        ):
            self.run_pipeline()
        self.assertEqual(list(original.iterdir()), [])
        self.assertEqual((self.claimed / "replacement").read_bytes(), b"keep")
        self.assert_verified_run_blocked()

    def test_cleanup_after_final_stat_cannot_recurse_into_a_replacement(self) -> None:
        original = self.staging / "displaced-original"
        real_stat = os.stat

        def swap_after_final_stat(path: str, **kwargs: Any) -> os.stat_result:
            info = real_stat(path, **kwargs)
            if path == NATIVE and not (self.claimed / "index").is_file():
                self.claimed.rename(original)
                self.claimed.mkdir()
                (self.claimed / "replacement").write_bytes(b"keep")
            return info

        with (
            patch.object(os, "stat", side_effect=swap_after_final_stat),
            self.assertRaises(OSError),
        ):
            self.run_pipeline()
        self.assertEqual(list(original.iterdir()), [])
        self.assertEqual((self.claimed / "replacement").read_bytes(), b"keep")
        self.assert_verified_run_blocked()

    def test_cleanup_recurses_but_does_not_follow_symlinks(self) -> None:
        chunks = self.native / "block" / "chunks"
        chunks.mkdir(parents=True)
        (chunks / "000001").write_bytes(b"data")
        sentinel = self.unrelated / "unrelated"
        sentinel.write_bytes(b"keep")
        (chunks / "linked").symlink_to(self.unrelated, target_is_directory=True)
        self.assertEqual(self.run_pipeline(), self.proof)
        self.assertFalse(self.staging.exists())
        self.assertFalse(self.native.exists())
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_partial_cleanup_keeps_claim_proof_pending_marker_and_failed_lock(
        self,
    ) -> None:
        (self.native / "meta.json").write_bytes(b"partial")

        def remove_part_then_fail(producer: ModuleType, fd: int, device: int) -> None:
            self.assertEqual(producer.directory_identity(os.fstat(fd)), (device, self.ino))
            os.unlink("index", dir_fd=fd)
            raise OSError

        with (
            patch.object(pipeline, "remove_children", side_effect=remove_part_then_fail),
            self.assertRaises(OSError),
        ):
            self.run_pipeline()
        self.assertFalse(self.native.exists())
        self.assertFalse((self.claimed / "index").exists())
        self.assertEqual((self.claimed / "meta.json").read_bytes(), b"partial")
        self.assert_verified_run_blocked()


class ApiTest(unittest.TestCase):
    def test_direct_tls_bounded_request_and_403_body_not_exposed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "token").write_text("synthetic-test-token\n")
            with (
                patch.object(shared, "SERVICE_ACCOUNT", root),
                patch.dict(
                    pipeline.os.environ,
                    {
                        "KUBERNETES_SERVICE_HOST": "10.0.0.1",
                        "KUBERNETES_SERVICE_PORT": "443",
                    },
                ),
                patch.object(shared.ssl, "create_default_context") as context,
                patch.object(shared, "HTTPSConnection") as https,
            ):
                api = pipeline.KubeApi()
                connection = https.return_value
                response = connection.getresponse.return_value.__enter__.return_value
                response.status = 403
                with self.assertRaisesRegex(
                    pipeline.ApiError, r"^Kubernetes GET /test: HTTP 403$"
                ):
                    api.request("/test")
                context.assert_called_once_with(cafile=str(root / "ca.crt"))
                https.assert_called_once_with(
                    "10.0.0.1", 443, timeout=15, context=context.return_value
                )
                connection.request.assert_called_once_with(
                    "GET",
                    "/test",
                    body=None,
                    headers={
                        "Authorization": "Bearer synthetic-test-token",
                        "Accept": "application/json",
                    },
                )
                connection.close.assert_called_once_with()
                response.read.assert_not_called()

                response.status = 201
                response.read.return_value = b'{"metadata":{"name":"fixture"}}'
                self.assertEqual(
                    api.request("/test", {}), {"metadata": {"name": "fixture"}}
                )
                response.read.assert_called_once_with(shared.MAX_RESPONSE + 1)

                connection.getresponse.side_effect = TimeoutError(
                    "synthetic-test-token"
                )
                with self.assertRaisesRegex(
                    pipeline.PipelineError,
                    r"^Kubernetes GET /test: invalid response or request failed$",
                ):
                    api.request("/test")


if __name__ == "__main__":
    unittest.main()
