"""Kubernetes transport, UID-pinned waits, and trusted restore Job clones."""

from contextlib import closing
from copy import deepcopy
from http.client import HTTPException, HTTPSConnection
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
import ssl
import time
from typing import Any, Callable

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
PREFIX = "platform.k3s-on-omv.io/"
RUN_TAG = PREFIX + "backup-run-tag"
UID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
JOB_LIMIT = 7200
WAIT_LIMIT = JOB_LIMIT + 300
POLL_SECONDS = 5
REQUEST_TIMEOUT = 15
MAX_RESPONSE = 1024 * 1024

Object = dict[str, Any]


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


def resource(
    namespace: str, api_version: str, kind: str, name: str, spec: Object
) -> Object:
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {"namespace": namespace, "name": name},
        "spec": spec,
    }


def k8up_resource(
    namespace: str,
    repository_secret: str,
    pod_config: str,
    kind: str,
    name: str,
    **extras: Any,
) -> Object:
    return resource(
        namespace,
        "k8up.io/v1",
        kind,
        name,
        {
            "backend": {
                "repoPasswordSecretRef": {"name": repository_secret, "key": "password"},
                "local": {"mountPath": "/repository"},
                "volumeMounts": [{"name": "repository", "mountPath": "/repository"}],
            },
            "podConfigRef": {"name": pod_config},
            "activeDeadlineSeconds": JOB_LIMIT,
            "failedJobsHistoryLimit": 3,
            "successfulJobsHistoryLimit": 3,
            **extras,
        },
    )


def identity(obj: Object, namespace: str, name: str) -> str:
    metadata = obj.get("metadata", {})
    uid = metadata.get("uid")
    if (
        metadata.get("name") != name
        or metadata.get("namespace") != namespace
        or metadata.get("deletionTimestamp") is not None
        or not isinstance(uid, str)
        or not UID.fullmatch(uid)
    ):
        raise PipelineError(f"invalid Kubernetes identity for {name}")
    return uid


def create_and_wait(
    api: KubeApi, collection: str, body: Object, ready: Callable[[Object], bool]
) -> str:
    namespace = body["metadata"]["namespace"]
    name = body["metadata"]["name"]
    uid = identity(api.request(collection, body), namespace, name)
    deadline = time.monotonic() + WAIT_LIMIT
    while time.monotonic() < deadline:
        obj = api.request(f"{collection}/{name}")
        if identity(obj, namespace, name) != uid:
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


def verify_complete(obj: Object, stage: str = "restore verifier") -> bool:
    status = conditions(obj)
    if status.get("Failed", {}).get("status") == "True":
        raise PipelineError(f"{stage} Job failed")
    return status.get("Complete", {}).get("status") == "True"


def clone_job(api: KubeApi, namespace: str, template_path: str, name: str) -> Object:
    cron = api.request(template_path)["spec"]
    if cron.get("suspend") is not True:
        raise PipelineError("restore verifier CronJob must be suspended")
    return resource(
        namespace, "batch/v1", "Job", name, deepcopy(cron["jobTemplate"]["spec"])
    )


def clone_verifier(
    api: KubeApi,
    namespace: str,
    template_path: str,
    name: str,
    inputs: tuple[tuple[str, str, str], ...],
    annotations: dict[str, str],
) -> Object:
    job = clone_job(api, namespace, template_path, name)
    template = job["spec"]["template"]
    containers = {item["name"]: item for item in template["spec"]["initContainers"]}
    for container, variable, annotation in inputs:
        # Fail closed against a template which does not require the exact run pins.
        paths = {
            env["name"]: env.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath")
            for env in containers.get(container, {}).get("env", [])
        }
        if paths.get(variable) != f"metadata.annotations['{annotation}']":
            raise PipelineError(
                "restore verifier template does not consume exact run inputs"
            )
    template.setdefault("metadata", {}).setdefault("annotations", {}).update(
        annotations
    )
    return job
