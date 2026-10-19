#!/usr/bin/env python3
"""OpenBao snapshot upload and count-retention adapter with fail-closed planning."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

KEY = re.compile(r"^(daily|weekly|monthly)/(\d{8}T\d{6}Z)\.snap$")
KEEP = {"daily": 7, "weekly": 4, "monthly": 6}
SHA256 = re.compile(r"[0-9a-f]{64}")


def required_prefixes(now: datetime) -> set[str]:
    required = {"daily"}
    if now.weekday() == 6:
        required.add("weekly")
    if now.day == 1:
        required.add("monthly")
    return required


def deletion_plan(
    keys: Iterable[str],
    verified_uploads: Mapping[str, Mapping[str, str]],
    *,
    required: set[str],
) -> list[str]:
    """Plan deletion only when each calendar-required object is checksum verified."""
    if not required or not required <= set(KEEP):
        raise ValueError("required prefixes must be a non-empty subset of retention prefixes")
    if set(verified_uploads) != required:
        raise ValueError("every required upload, and no unrelated upload, must be verified")

    grouped: dict[str, list[tuple[str, str]]] = {name: [] for name in KEEP}
    all_keys: set[str] = set()
    for key in keys:
        match = KEY.fullmatch(key)
        if match is None:
            raise ValueError(f"unrecognized snapshot object: {key}")
        grouped[match.group(1)].append((match.group(2), key))
        all_keys.add(key)

    for prefix, proof in verified_uploads.items():
        key = proof.get("key", "")
        digest = proof.get("sha256", "")
        if key not in all_keys or not key.startswith(f"{prefix}/") or SHA256.fullmatch(digest) is None:
            raise ValueError("upload proof must identify a listed object and valid SHA-256")

    delete: list[str] = []
    for prefix in required:
        ordered = sorted(grouped[prefix], reverse=True)
        delete.extend(key for _, key in ordered[KEEP[prefix] :])
    return sorted(delete)


def _request(url: str, *, method: str, body: dict | None = None, token: str | None = None) -> bytes:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    payload = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers, method=method), timeout=60) as response:
        return response.read()


def execute(
    *,
    now: datetime,
    workdir: Path,
    request: Callable[..., bytes] = _request,
    s3_client=None,
) -> list[str]:
    """Create, upload, verify, and then delete only planner-approved objects."""
    address = os.environ["OPENBAO_ADDR"].rstrip("/")
    endpoint = os.environ["S3_ENDPOINT"]
    bucket = os.environ["S3_BUCKET"]
    if s3_client is None:
        import boto3

        s3_client = boto3.client("s3", endpoint_url=endpoint, region_name=os.environ.get("AWS_REGION", "auto"))
    jwt = Path(os.getenv("KUBERNETES_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token")).read_text().strip()
    login = json.loads(request(f"{address}/v1/auth/kubernetes/login", method="POST", body={"role": "openbao-snapshot", "jwt": jwt}))
    token = login["auth"]["client_token"]

    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    workdir.mkdir(parents=True, exist_ok=True)
    snapshot_path = workdir / f"{timestamp}.snap"
    snapshot_path.write_bytes(request(f"{address}/v1/sys/storage/raft/snapshot", method="GET", token=token))
    digest = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    checksum_path = workdir / f"{timestamp}.snap.sha256"
    checksum_path.write_text(f"{digest}  {timestamp}.snap\n")

    required = required_prefixes(now)
    proofs: dict[str, dict[str, str]] = {}
    for prefix in sorted(required):
        key = f"{prefix}/{timestamp}.snap"
        s3_client.upload_file(str(snapshot_path), bucket, key)
        s3_client.upload_file(str(checksum_path), bucket, f"{key}.sha256")
        downloaded = workdir / f"verify-{prefix}.snap"
        s3_client.download_file(bucket, key, str(downloaded))
        if hashlib.sha256(downloaded.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"checksum verification failed for {prefix} upload")
        proofs[prefix] = {"key": key, "sha256": digest}

    listed: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for prefix in KEEP:
        for payload in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
            listed.extend(item["Key"] for item in payload.get("Contents", []) if item["Key"].endswith(".snap"))

    plan = deletion_plan(listed, proofs, required=required)
    for key in plan:
        s3_client.delete_object(Bucket=bucket, Key=key)
        s3_client.delete_object(Bucket=bucket, Key=f"{key}.sha256")
    return plan


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "run":
        execute(now=datetime.now(timezone.utc), workdir=Path(os.getenv("WORKDIR", "/work")))
        return 0
    payload = json.load(sys.stdin)
    plan = deletion_plan(payload["keys"], payload["verified_uploads"], required=set(payload["required_prefixes"]))
    json.dump({"delete": plan}, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
