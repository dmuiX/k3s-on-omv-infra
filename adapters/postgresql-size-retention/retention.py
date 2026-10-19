#!/usr/bin/env python3
"""Fail-closed state transitions for SPEC v6 Barman size retention."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import urllib.request

BUDGET = 4 * 1024**3
MAX_LEASE_AGE = timedelta(days=8)


@dataclass(frozen=True)
class Backup:
    backup_id: str
    completed_at: datetime
    base_bytes: int
    status: str = "DONE"
    archival_protected: bool = False


def _fingerprint(backups: list[Backup]) -> tuple[tuple[str, datetime, int, str, bool], ...]:
    return tuple(sorted((b.backup_id, b.completed_at, b.base_bytes, b.status, b.archival_protected) for b in backups))


def plan_dry_run(
    backups: list[Backup],
    lease_result: str,
    lease_renewed: datetime,
    *,
    now: datetime | None = None,
    archiver_stale: bool = False,
    backup_running: bool = False,
    catalog_matches_storage: bool = False,
    acceptance: bool = False,
) -> tuple[str, list[str]]:
    """Return at most one dry-run command; never authorize destructive execution."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or lease_renewed.tzinfo is None:
        return "blocked: naive timestamp", []
    lease_age = now - lease_renewed
    if not acceptance:
        return "blocked: isolated acceptance missing", []
    if lease_result != "success" or lease_age < timedelta(0) or lease_age > MAX_LEASE_AGE:
        return "blocked: restore lease failed, future-dated, or stale", []
    if archiver_stale or backup_running:
        return "blocked: archiver stale or backup running", []
    if not catalog_matches_storage:
        return "blocked: catalog and object listing differ", []
    if (
        any(b.status != "DONE" or b.base_bytes < 0 or not b.backup_id or b.completed_at.tzinfo is None for b in backups)
        or len({b.backup_id for b in backups}) != len(backups)
    ):
        return "blocked: unknown catalog", []

    ordered = sorted(backups, key=lambda b: b.completed_at, reverse=True)
    if sum(b.base_bytes for b in ordered) <= BUDGET:
        return "within budget", []
    if len(ordered) <= 2:
        return "budget blocked by minimum redundancy", []

    protected_ids = {b.backup_id for b in ordered[:2]}
    candidates = [b for b in reversed(ordered) if not b.archival_protected and b.backup_id not in protected_ids]
    if not candidates:
        return "budget blocked: no unprotected backup", []
    candidate = candidates[0].backup_id
    return "dry-run one oldest unprotected backup", ["barman-cloud-backup-delete", "--dry-run", "--backup-id", candidate, "--minimum-redundancy", "2"]


def authorize_after_dry_run(before: list[Backup], after: list[Backup], dry_run_command: list[str], *, dry_run_succeeded: bool) -> list[str]:
    """Authorize deletion only if a successful dry-run left an identical catalog."""
    if not dry_run_succeeded or _fingerprint(before) != _fingerprint(after):
        return []
    if len(dry_run_command) != 6 or dry_run_command[:2] != ["barman-cloud-backup-delete", "--dry-run"]:
        return []
    if dry_run_command[2] != "--backup-id" or dry_run_command[4:] != ["--minimum-redundancy", "2"]:
        return []
    return [dry_run_command[0], *dry_run_command[2:]]


def verify_post_delete(before: list[Backup], after: list[Backup], deleted_id: str) -> bool:
    """Require exactly the selected base backup to disappear before reporting success."""
    before_ids = {b.backup_id for b in before}
    after_ids = {b.backup_id for b in after}
    return deleted_id in before_ids and before_ids - after_ids == {deleted_id} and len(after_ids) >= 2


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks a timezone")
    return parsed


def _run(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=300).stdout


def _lease() -> tuple[str, datetime]:
    namespace = os.environ["RESTORE_LEASE_NAMESPACE"]
    name = os.environ["RESTORE_LEASE_NAME"]
    token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
    context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    request = urllib.request.Request(
        f"https://kubernetes.default.svc/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases/{name}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, context=context, timeout=15) as response:
        payload = json.load(response)
    annotations = payload.get("metadata", {}).get("annotations", {})
    if annotations.get("k3s-on-omv.io/pitr-verified") != "true" or annotations.get("k3s-on-omv.io/omv-mirror-verified") != "true":
        raise RuntimeError("restore evidence does not cover PITR and the OMV mirror")
    renewed = payload.get("spec", {}).get("renewTime")
    if not renewed:
        raise RuntimeError("restore evidence has no renewal timestamp")
    return annotations.get("k3s-on-omv.io/result", ""), _parse_time(renewed)


def _barman_base_args() -> list[str]:
    endpoint = os.environ["S3_ENDPOINT"]
    bucket = os.environ["S3_BUCKET"]
    server = os.environ["BARMAN_SERVER_NAME"]
    if not endpoint.startswith("https://") or not bucket or not server:
        raise RuntimeError("invalid Barman target")
    return ["--endpoint-url", endpoint, f"s3://{bucket}", server]


def _catalog() -> list[Backup]:
    payload = json.loads(_run(["barman-cloud-backup-list", "--format", "json", *_barman_base_args()]))
    records = payload.get("backups_list")
    if not isinstance(records, list):
        raise RuntimeError("Barman returned an unknown catalog")
    backups = []
    for record in records:
        backups.append(
            Backup(
                backup_id=str(record.get("backup_id", "")),
                completed_at=_parse_time(str(record.get("end_time", ""))),
                base_bytes=int(record.get("size", -1)),
                status=str(record.get("status", "")),
                archival_protected=bool(record.get("is_archival", False)),
            )
        )
    return backups


def _storage_state(backups: list[Backup]) -> tuple[bool, bool]:
    import boto3

    client = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], region_name=os.environ.get("AWS_REGION", "auto"))
    bucket = os.environ["S3_BUCKET"]
    server = os.environ["BARMAN_SERVER_NAME"]
    base_ids: set[str] = set()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{server}/base/"):
        for item in page.get("Contents", []):
            parts = item["Key"].split("/")
            if len(parts) >= 4 and parts[-1] == "backup.info":
                base_ids.add(parts[-2])
    catalog_ids = {backup.backup_id for backup in backups}

    latest_wal = None
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{server}/wals/"):
        for item in page.get("Contents", []):
            modified = item.get("LastModified")
            if modified is not None and (latest_wal is None or modified > latest_wal):
                latest_wal = modified
    archiver_stale = latest_wal is None or datetime.now(timezone.utc) - latest_wal > timedelta(hours=1)
    return base_ids == catalog_ids, archiver_stale


def run() -> None:
    if os.environ.get("DELETION_ACCEPTANCE") != "true":
        raise RuntimeError("isolated destructive-retention acceptance is not enabled")
    if int(os.environ["BASE_BACKUP_BUDGET_BYTES"]) != BUDGET:
        raise RuntimeError("base-backup budget differs from the reviewed contract")

    lease_result, lease_renewed = _lease()
    before = _catalog()
    catalog_matches, archiver_stale = _storage_state(before)
    reason, dry_run = plan_dry_run(
        before,
        lease_result,
        lease_renewed,
        archiver_stale=archiver_stale,
        backup_running=any(backup.status not in {"DONE", "FAILED"} for backup in before),
        catalog_matches_storage=catalog_matches,
        acceptance=True,
    )
    if not dry_run:
        if reason == "within budget":
            print(reason)
            return
        raise RuntimeError(reason)

    command = [*dry_run, *_barman_base_args()]
    _run(command)
    after_dry_run = _catalog()
    destructive = authorize_after_dry_run(before, after_dry_run, dry_run, dry_run_succeeded=True)
    if not destructive:
        raise RuntimeError("dry-run did not preserve the catalog")
    _run([*destructive, *_barman_base_args()])
    after_delete = _catalog()
    deleted_id = dry_run[3]
    if not verify_post_delete(before, after_delete, deleted_id):
        raise RuntimeError("post-delete catalog verification failed")
    print("deleted one verified obsolete base backup")


if __name__ == "__main__":
    try:
        if sys.argv[1:] != ["run"]:
            raise RuntimeError("expected the run subcommand")
        run()
    except Exception as exc:
        print(f"retention blocked: {exc}", file=sys.stderr)
        raise SystemExit(1)
