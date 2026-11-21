"""Supervise one exact K8up Backup, Check, and offline Prometheus restore."""

from contextlib import ExitStack, contextmanager
from functools import partial
from importlib import import_module
import json
import os
from pathlib import Path
import stat
import sys
from types import ModuleType
from typing import Iterator

if __name__ == "__main__":
    sys.path.insert(0, "/runner")

from k8up_pipeline import (
    ApiError,
    KubeApi,
    Object,
    PipelineError,
    PREFIX,
    RUN_TAG,
    UID,
    clone_verifier,
    conditions,
    create_and_wait,
    k8up_complete,
    k8up_resource as repository_resource,
    verify_complete,
)

NAMESPACE = "kube-prometheus-stack"
K8UP = f"/apis/k8up.io/v1/namespaces/{NAMESPACE}"
BATCH = f"/apis/batch/v1/namespaces/{NAMESPACE}"
PRODUCER = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/prometheus-tsdb-snapshot"
TEMPLATE = f"{BATCH}/cronjobs/prometheus-restore-test"
LOCK = ".k8up-pipeline-lock"
CLEANUP_PREFIX = ".k8up-cleanup-"
NATIVE_SNAPSHOT = PREFIX + "native-snapshot"
Binding = tuple[str, tuple[int, int]]
k8up_resource = partial(
    repository_resource, NAMESPACE, "prometheus-k8up-repository", "prometheus-k8up-omv"
)


def backup_complete(api: KubeApi, obj: Object) -> bool:
    complete = k8up_complete(obj)
    producer = conditions(obj).get("PreBackupPodReady", {})
    reason = producer.get("reason")
    if reason in {
        "Failed",
        "RetrievalFailed",
        "DeletionFailed",
        "NoPreBackupPodsFound",
    }:
        raise PipelineError("K8up producer did not complete")
    if not complete or producer.get("status") != "True" or reason != "Finished":
        return False
    # K8up v2.16 reports Finished after requesting foreground deletion. Wait
    # for the Deployment (and its dependent producer Pods) to actually be gone.
    try:
        api.request(PRODUCER)
    except ApiError as exc:
        if exc.status == 404:
            return True
        raise
    return False


def verifier(api: KubeApi, name: str, run_tag: str, native: str) -> Object:
    return clone_verifier(
        api,
        NAMESPACE,
        TEMPLATE,
        name,
        (
            ("restore", "K8UP_PROMETHEUS_RUN_TAG", RUN_TAG),
            ("restore", "PROMETHEUS_EXPECTED_SNAPSHOT", NATIVE_SNAPSHOT),
            ("validate-archive", "PROMETHEUS_EXPECTED_SNAPSHOT", NATIVE_SNAPSHOT),
        ),
        {RUN_TAG: run_tag, NATIVE_SNAPSHOT: native},
    )


@contextmanager
def bound_snapshot(
    producer: ModuleType, root_fd: int, backup_uid: str
) -> Iterator[Binding]:
    """Read the exact ready binding and pin its inode throughout verification."""
    with ExitStack() as stack:
        try:
            binding: Binding = producer.read_binding(root_fd, backup_uid)
            native, original = binding
            fd = stack.enter_context(producer.directory_fd(native, dir_fd=root_fd))
            if producer.directory_identity(os.fstat(fd)) != original:
                raise ValueError
        except (OSError, ValueError):
            raise PipelineError(
                "producer did not leave the exact ready binding"
            ) from None
        yield binding


def require_binding(
    producer: ModuleType, root_fd: int, backup_uid: str, expected: Binding
) -> None:
    try:
        if producer.read_binding(root_fd, backup_uid) != expected:
            raise ValueError
    except (OSError, ValueError):
        raise PipelineError(
            "final producer binding does not match; cleanup blocked"
        ) from None


def write_once(producer: ModuleType, root_fd: int, name: str, payload: Object) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd)
    with os.fdopen(fd, "wb") as file:
        producer.write_durable(
            file, (json.dumps(payload, sort_keys=True) + "\n").encode("ascii")
        )
    os.fsync(root_fd)


def remove_children(producer: ModuleType, directory_fd: int, device: int) -> None:
    """Traverse only opened directories; unlink symlinks without following them."""
    for name in os.listdir(directory_fd):
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if entry.st_dev != device:
            raise PipelineError("snapshot cleanup entry is not on the same volume")
        if not stat.S_ISDIR(entry.st_mode):
            os.unlink(name, dir_fd=directory_fd)
            continue
        with producer.directory_fd(name, dir_fd=directory_fd) as child_fd:
            if not os.path.samestat(entry, os.fstat(child_fd)):
                raise PipelineError("snapshot cleanup directory changed before traversal")
            remove_children(producer, child_fd, device)
            if not os.path.samestat(
                entry, os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            ):
                raise PipelineError("snapshot cleanup directory changed before removal")
            os.rmdir(name, dir_fd=directory_fd)


def claim_and_remove(
    producer: ModuleType, root_fd: int, staging: str, binding: Binding
) -> None:
    native, original = binding
    # Never reuse a prior claim. A rename under this root is atomic, with no copy fallback.
    os.mkdir(staging, 0o700, dir_fd=root_fd)
    with producer.directory_fd(staging, dir_fd=root_fd) as stage_fd:
        stage_info = os.fstat(stage_fd)
        if stage_info.st_dev != os.fstat(root_fd).st_dev:
            raise PipelineError("snapshot cleanup staging is not on the same volume")
        os.rename(native, native, src_dir_fd=root_fd, dst_dir_fd=stage_fd)
        claimed = os.stat(native, dir_fd=stage_fd, follow_symlinks=False)
        # Persist the claim for recovery even if it turns out to hold a replacement.
        os.fsync(stage_fd)
        os.fsync(root_fd)
        if producer.directory_identity(claimed) != original:
            raise PipelineError("claimed snapshot directory identity does not match")
        with producer.directory_fd(native, dir_fd=stage_fd) as native_fd:
            if producer.directory_identity(os.fstat(native_fd)) != original:
                raise PipelineError("claimed snapshot directory changed before deletion")
            # Never traverse from the staged name again after checking the opened fd.
            remove_children(producer, native_fd, original[0])
            current = os.stat(native, dir_fd=stage_fd, follow_symlinks=False)
            if producer.directory_identity(current) != original:
                raise PipelineError("claimed snapshot directory changed before removal")
            os.rmdir(native, dir_fd=stage_fd)
        os.fsync(stage_fd)
        if not os.path.samestat(
            stage_info, os.stat(staging, dir_fd=root_fd, follow_symlinks=False)
        ):
            raise PipelineError("snapshot cleanup staging was replaced")
        os.rmdir(staging, dir_fd=root_fd)
        os.fsync(root_fd)


def run(api: KubeApi, producer: ModuleType, root: Path, pod_uid: str) -> Path:
    """Leave the lock in place unless verification and cleanup both succeed."""
    if not UID.fullmatch(pod_uid):
        raise PipelineError("missing or invalid runner Pod UID")
    with producer.directory_fd(root) as root_fd:
        try:
            write_once(producer, root_fd, LOCK, {"version": 1, "pod_uid": pod_uid})
        except FileExistsError:
            raise PipelineError(
                "previous pipeline run is blocked; lock retained"
            ) from None
        try:
            os.stat(producer.PENDING, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise PipelineError(
                "previous producer binding is pending; no Backup submitted"
            )

        run_tag = f"backup-run-{pod_uid}"
        # K8up uses backup-<name>-prebackup as a Job name and label (63 bytes max).
        backup = k8up_resource(
            "Backup",
            f"pt-{pod_uid}",
            labelSelectors=[
                {"matchLabels": {PREFIX + "backup-set": "prometheus-tsdb"}}
            ],
            tags=["prometheus-tsdb", "prometheus-replica-0", run_tag],
        )
        backup_uid = create_and_wait(
            api, f"{K8UP}/backups", backup, lambda obj: backup_complete(api, obj)
        )
        with bound_snapshot(producer, root_fd, backup_uid) as binding:
            native, (dev, ino) = binding
            check = k8up_resource("Check", f"prometheus-check-{pod_uid}")
            check_uid = create_and_wait(api, f"{K8UP}/checks", check, k8up_complete)
            job = verifier(api, f"prometheus-verify-{pod_uid}", run_tag, native)
            job_uid = create_and_wait(api, f"{BATCH}/jobs", job, verify_complete)

            require_binding(producer, root_fd, backup_uid, binding)
            proof = f".k8up-verified-{pod_uid}.json"
            staging = f"{CLEANUP_PREFIX}{pod_uid}"
            write_once(
                producer,
                root_fd,
                proof,
                {
                    "version": 1,
                    "pod_uid": pod_uid,
                    "backup_uid": backup_uid,
                    "check_uid": check_uid,
                    "verifier_uid": job_uid,
                    "run_tag": run_tag,
                    "snapshot_name": native,
                    "st_dev": dev,
                    "st_ino": ino,
                    "staging_directory": staging,
                },
            )
            claim_and_remove(producer, root_fd, staging, binding)
        require_binding(producer, root_fd, backup_uid, binding)
        os.unlink(producer.PENDING, dir_fd=root_fd)
        os.fsync(root_fd)
        # Release the lock last. An interruption can only leave a stale lock.
        os.unlink(LOCK, dir_fd=root_fd)
    return root / proof


def main() -> None:
    try:
        # The runner uses Python isolated mode; import the separately mounted contract.
        sys.path.insert(0, "/producer")
        producer = import_module("snapshot")
        proof = run(
            KubeApi(), producer, Path("/snapshots"), os.environ.get("POD_UID", "")
        )
    except PipelineError as exc:
        sys.exit(f"Prometheus backup pipeline failed: {exc}")
    except (
        ImportError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        sys.exit(f"Prometheus backup pipeline failed: {type(exc).__name__}")
    print(f"stage=cleanup result=PASS proof={proof.name}", flush=True)


if __name__ == "__main__":
    main()
