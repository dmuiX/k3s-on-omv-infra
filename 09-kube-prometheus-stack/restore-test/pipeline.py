"""Supervise one exact K8up Backup, Check, and offline Prometheus restore."""

from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
from http.client import HTTPException, HTTPSConnection
from importlib import import_module
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import time
from types import ModuleType
from typing import Any, Callable, Iterator

NAMESPACE = "kube-prometheus-stack"
K8UP = f"/apis/k8up.io/v1/namespaces/{NAMESPACE}"
BATCH = f"/apis/batch/v1/namespaces/{NAMESPACE}"
PRODUCER = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/prometheus-tsdb-snapshot"
TEMPLATE = f"{BATCH}/cronjobs/prometheus-restore-test"
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
LOCK = ".k8up-pipeline-lock"
CLEANUP_PREFIX = ".k8up-cleanup-"
PREFIX = "platform.k3s-on-omv.io/"
RUN_TAG = PREFIX + "backup-run-tag"
NATIVE_SNAPSHOT = PREFIX + "native-snapshot"
UID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
JOB_LIMIT = 7200
WAIT_LIMIT = JOB_LIMIT + 300
POLL_SECONDS = 5
REQUEST_TIMEOUT = 15
MAX_RESPONSE = 1024 * 1024

Object = dict[str, Any]
Binding = tuple[str, tuple[int, int]]


class PipelineError(RuntimeError):
    """A safe diagnostic that never contains API bodies or credentials."""


class ApiError(PipelineError):
    def __init__(self, method: str, path: str, status: int) -> None:
        super().__init__(f"Kubernetes {method} {path}: HTTP {status}")
        self.status = status


class KubeApi:
    """Use the in-cluster endpoint and CA directly, without redirects or proxies."""

    def __init__(self) -> None:
        self.host = str(ip_address(os.environ["KUBERNETES_SERVICE_HOST"]))
        self.port = int(os.environ["KUBERNETES_SERVICE_PORT"])
        self.context = ssl.create_default_context(
            cafile=str(SERVICE_ACCOUNT / "ca.crt")
        )

    def request(self, path: str, body: Object | None = None) -> Object:
        method = "GET" if body is None else "POST"
        try:
            # Re-read the projected token so rotation does not break a long run.
            token = (SERVICE_ACCOUNT / "token").read_text().strip()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("ascii")
                headers["Content-Type"] = "application/json"
            with closing(
                HTTPSConnection(
                    self.host, self.port, timeout=REQUEST_TIMEOUT, context=self.context
                )
            ) as connection:
                connection.request(method, path, body=payload, headers=headers)
                with connection.getresponse() as response:
                    if response.status != (200 if body is None else 201):
                        raise ApiError(method, path, response.status)
                    data = response.read(MAX_RESPONSE + 1)
            if len(data) <= MAX_RESPONSE and isinstance(
                result := json.loads(data), dict
            ):
                return result
        except (HTTPException, OSError, ValueError):
            pass
        raise PipelineError(
            f"Kubernetes {method} {path}: invalid response or request failed"
        )


def resource(api_version: str, kind: str, name: str, spec: Object) -> Object:
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {"namespace": NAMESPACE, "name": name},
        "spec": spec,
    }


def k8up_resource(kind: str, name: str, **extras: Any) -> Object:
    return resource(
        "k8up.io/v1",
        kind,
        name,
        {
            "backend": {
                "repoPasswordSecretRef": {
                    "name": "prometheus-k8up-repository",
                    "key": "password",
                },
                "local": {"mountPath": "/repository"},
                "volumeMounts": [{"name": "repository", "mountPath": "/repository"}],
            },
            "podConfigRef": {"name": "prometheus-k8up-omv"},
            "activeDeadlineSeconds": JOB_LIMIT,
            "failedJobsHistoryLimit": 3,
            "successfulJobsHistoryLimit": 3,
            **extras,
        },
    )


def identity(obj: Object, name: str) -> str:
    metadata = obj.get("metadata", {})
    uid = metadata.get("uid")
    if (
        metadata.get("name") != name
        or metadata.get("namespace") != NAMESPACE
        or metadata.get("deletionTimestamp") is not None
        or not isinstance(uid, str)
        or not UID.fullmatch(uid)
    ):
        raise PipelineError(f"invalid Kubernetes identity for {name}")
    return uid


def create_and_wait(
    api: KubeApi, collection: str, body: Object, ready: Callable[[Object], bool]
) -> str:
    name = body["metadata"]["name"]
    uid = identity(api.request(collection, body), name)
    deadline = time.monotonic() + WAIT_LIMIT
    while time.monotonic() < deadline:
        obj = api.request(f"{collection}/{name}")
        if identity(obj, name) != uid:
            raise PipelineError(f"Kubernetes object replaced while waiting: {name}")
        done = ready(obj)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if done:
            return uid
        time.sleep(min(POLL_SECONDS, remaining))
    raise PipelineError(f"timed out waiting for {name}")


def conditions(obj: Object) -> dict[str, Object]:
    return {
        entry["type"]: entry for entry in obj.get("status", {}).get("conditions", [])
    }


def k8up_complete(obj: Object) -> bool:
    completed = conditions(obj).get("Completed", {})
    if completed.get("status") != "True":
        return False
    if completed.get("reason") != "Succeeded":
        raise PipelineError("K8up operation did not succeed")
    return True


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


def verify_complete(obj: Object) -> bool:
    status = conditions(obj)
    if status.get("Failed", {}).get("status") == "True":
        raise PipelineError("restore verifier Job failed")
    return status.get("Complete", {}).get("status") == "True"


def verifier(api: KubeApi, name: str, run_tag: str, native: str) -> Object:
    cron = api.request(TEMPLATE)["spec"]
    if cron.get("suspend") is not True:
        raise PipelineError("restore verifier CronJob must be suspended")
    job = deepcopy(cron["jobTemplate"]["spec"])
    template = job["template"]
    containers = {item["name"]: item for item in template["spec"]["initContainers"]}
    for container, variable, annotation in (
        ("restore", "K8UP_PROMETHEUS_RUN_TAG", RUN_TAG),
        ("restore", "PROMETHEUS_EXPECTED_SNAPSHOT", NATIVE_SNAPSHOT),
        ("validate-archive", "PROMETHEUS_EXPECTED_SNAPSHOT", NATIVE_SNAPSHOT),
    ):
        # Fail closed if a rollout left the old, latest-snapshot template installed.
        paths = {
            env["name"]: env.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath")
            for env in containers.get(container, {}).get("env", [])
        }
        if paths.get(variable) != f"metadata.annotations['{annotation}']":
            raise PipelineError(
                "restore verifier template does not consume exact run inputs"
            )
    template.setdefault("metadata", {}).setdefault("annotations", {}).update(
        {
            RUN_TAG: run_tag,
            NATIVE_SNAPSHOT: native,
        }
    )
    return resource("batch/v1", "Job", name, job)


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
