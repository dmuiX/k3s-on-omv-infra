"""Report recovered Deployment pods that were terminated by a node shutdown."""

import argparse
import json
import ssl
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from http.client import HTTPSConnection, HTTPException
from pathlib import Path
from typing import Any

Document = dict[str, Any]
Read = Callable[[str], Document]
Delete = Callable[[str, str, str], None]
TARGETS = {
    "argocd": {"argocd-dex-server"},
    "audiobookshelf": {"audiobookshelf"},
    "kube-prometheus-stack": {"kube-prometheus-stack-kube-state-metrics"},
    "kube-system": {"csi-smb-controller"},
    "longhorn": {"csi-attacher", "csi-provisioner", "csi-resizer", "csi-snapshotter"},
    "openbao": {"openbao-agent-injector"},
    "tekton-pipelines": {"tekton-dashboard", "tekton-pipelines-webhook"},
}
SHUTDOWN = "Pod was terminated in response to imminent node shutdown."


def owner_uid(resource: Document, kind: str) -> str | None:
    """Return the UID of the single controlling owner of the specified kind."""
    owners = [
        ref
        for ref in resource["metadata"].get("ownerReferences", [])
        if ref.get("controller") is True
    ]
    if len(owners) != 1 or owners[0].get("kind") != kind:
        return None
    uid = owners[0].get("uid")
    return uid if isinstance(uid, str) and uid else None


def recovered(deployment: Document) -> bool:
    """Require the current Deployment generation fully updated and available."""
    meta, spec = deployment["metadata"], deployment["spec"]
    status = deployment.get("status", {})
    desired = spec.get("replicas", 1)
    return bool(
        desired > 0
        and not meta.get("deletionTimestamp")
        and not spec.get("paused")
        and status.get("observedGeneration", 0) >= meta["generation"]
        # Avoid counting an old available replica during a surge rollout.
        and status.get("replicas", 0) == desired
        and status.get("updatedReplicas", 0) == desired
        and status.get("availableReplicas", 0) == desired
    )


def old_shutdown(pod: Document, cutoff: datetime) -> bool:
    """Match only explicit node shutdowns whose containers ended before cutoff."""
    status = pod.get("status", {})
    if (
        status.get("phase") != "Failed"
        or status.get("reason") != "Terminated"
        or status.get("message") != SHUTDOWN
        or pod["metadata"].get("deletionTimestamp")
    ):
        return False
    statuses = status.get("containerStatuses", [])
    if not statuses:
        return False
    statuses = (
        statuses
        + status.get("initContainerStatuses", [])
        + status.get("ephemeralContainerStatuses", [])
    )
    for item in statuses:
        finished = item.get("state", {}).get("terminated", {}).get("finishedAt")
        if not isinstance(finished, str):
            return False
        try:
            ended = datetime.fromisoformat(finished)
        except ValueError:
            return False
        if ended.tzinfo is None or ended.year <= 1970 or ended > cutoff:
            return False
    return True


def items(read: Read, path: str, namespace: str) -> Iterator[Document]:
    """Read one complete namespaced list; reject partial or mismatched results."""
    result = read(path)
    if result.get("metadata", {}).get("continue"):
        raise ValueError("unexpected partial Kubernetes list")
    for item in result["items"]:
        if item["metadata"]["namespace"] != namespace:
            raise ValueError("unexpected resource namespace")
        yield item


def preview(read: Read, *, now: datetime) -> list[dict[str, str]]:
    """Return candidate identities using a read-only, injectable API boundary."""
    candidates = []
    cutoff = now - timedelta(hours=1)
    for namespace, allowlist in TARGETS.items():
        core = f"/api/v1/namespaces/{namespace}"
        apps = f"/apis/apps/v1/namespaces/{namespace}"
        pods = [
            pod
            for pod in items(
                read, core + "/pods?fieldSelector=status.phase%3DFailed", namespace
            )
            if old_shutdown(pod, cutoff) and owner_uid(pod, "ReplicaSet")
        ]
        if not pods:
            continue
        replica_sets = {
            rs["metadata"]["uid"]: owner_uid(rs, "Deployment")
            for rs in items(read, apps + "/replicasets", namespace)
        }
        deployments = {
            dep["metadata"]["uid"]: dep["metadata"]["name"]
            for dep in items(read, apps + "/deployments", namespace)
            if dep["metadata"]["name"] in allowlist and recovered(dep)
        }
        for pod in pods:
            rs_uid = owner_uid(pod, "ReplicaSet")
            deployment_uid = replica_sets.get(rs_uid) if rs_uid else None
            deployment = deployments.get(deployment_uid) if deployment_uid else None
            if deployment:
                meta = pod["metadata"]
                candidates.append(
                    {
                        "namespace": namespace,
                        "pod": meta["name"],
                        "uid": meta["uid"],
                        "deployment": deployment,
                    }
                )
    return sorted(candidates, key=lambda item: (item["namespace"], item["pod"]))


def decode(body: bytes) -> Document:
    """Decode a Kubernetes API JSON object."""
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("expected a Kubernetes object")
    return result


def cluster_read(path: str) -> Document:
    """Use a TLS-verified, in-cluster GET without following redirects."""
    mount = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    tls = ssl.create_default_context(cafile=str(mount / "ca.crt"))
    auth = "Bearer " + (mount / "token").read_text(encoding="utf-8").strip()
    connection = HTTPSConnection("kubernetes.default.svc", context=tls, timeout=10)
    try:
        connection.request("GET", path, headers={"Authorization": auth})
        with connection.getresponse() as response:
            if response.status != 200:
                raise HTTPException(f"unexpected API status {response.status}")
            return decode(response.read())
    finally:
        connection.close()


def cluster_delete(namespace: str, pod: str, uid: str) -> None:
    """Delete one verified candidate pod in-cluster guarded by its exact UID."""
    mount = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    tls = ssl.create_default_context(cafile=str(mount / "ca.crt"))
    auth = "Bearer " + (mount / "token").read_text(encoding="utf-8").strip()
    path = f"/api/v1/namespaces/{namespace}/pods/{pod}"
    body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"uid": uid},
        }
    ).encode("utf-8")
    connection = HTTPSConnection("kubernetes.default.svc", context=tls, timeout=10)
    try:
        connection.request(
            "DELETE",
            path,
            body=body,
            headers={
                "Authorization": auth,
                "Content-Type": "application/json",
            },
        )
        with connection.getresponse() as response:
            if response.status not in (200, 202, 404):
                raise HTTPException(f"unexpected API delete status {response.status}")
            response.read()
    finally:
        connection.close()


def cleanup(
    read: Read,
    *,
    now: datetime,
    delete: Delete | None = None,
) -> list[dict[str, str]]:
    """Select verified shutdown candidates and optionally delete them by UID."""
    candidates = preview(read, now=now)
    if delete is not None:
        for candidate in candidates:
            delete(candidate["namespace"], candidate["pod"], candidate["uid"])
    return candidates


def main() -> int:
    """Report or delete verified node-shutdown pod leftovers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", help="Explicit context for local kubectl run")
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete verified candidates (guarded by UID precondition in-cluster)",
    )
    args = parser.parse_args()
    context: str | None = args.context
    do_delete: bool = args.delete

    def local_read(path: str) -> Document:
        assert context is not None
        result = subprocess.run(
            [
                "kubectl",
                "--context",
                context,
                "--request-timeout=10s",
                "get",
                "--raw",
                path,
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        return decode(result.stdout)

    def local_delete(namespace: str, pod: str, _uid: str) -> None:
        assert context is not None
        subprocess.run(
            [
                "kubectl",
                "--context",
                context,
                "--request-timeout=10s",
                "delete",
                "pod",
                "-n",
                namespace,
                pod,
                "--ignore-not-found=true",
                "--wait=false",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )

    delete_fn: Delete | None = None
    if do_delete:
        delete_fn = local_delete if context else cluster_delete

    try:
        candidates = cleanup(
            local_read if context else cluster_read,
            now=datetime.now(timezone.utc),
            delete=delete_fn,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        HTTPException,
        subprocess.SubprocessError,
    ) as error:
        print(
            f"Cleanup failed ({type(error).__name__}); aborting",
            file=sys.stderr,
        )
        return 1
    for candidate in candidates:
        print(json.dumps(candidate, sort_keys=True))
    if do_delete:
        print(f"Cleanup: deleted {len(candidates)} candidate(s)")
    else:
        print(f"Preview: {len(candidates)} candidate(s); nothing deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
