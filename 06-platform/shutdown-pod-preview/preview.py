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
TARGETS = {
    "argocd": {"argocd-dex-server"},
    "audiobookshelf": {"audiobookshelf"},
    "kube-system": {"csi-smb-controller"},
    "longhorn": {"csi-attacher", "csi-provisioner", "csi-resizer", "csi-snapshotter"},
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


def main() -> int:
    """Print a preview without executing or supporting any write operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", help="Explicit context for local kubectl preview")
    context = parser.parse_args().context

    def local_read(path: str) -> Document:
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

    try:
        candidates = preview(
            local_read if context else cluster_read, now=datetime.now(timezone.utc)
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
            f"Preview failed ({type(error).__name__}); no candidate set emitted",
            file=sys.stderr,
        )
        return 1
    for candidate in candidates:
        print(json.dumps(candidate, sort_keys=True))
    print(f"Preview: {len(candidates)} candidate(s); nothing deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
