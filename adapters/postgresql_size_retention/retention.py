"""Fail-closed Barman base-backup size retention.

R2/object storage is read-only to this adapter.  Productive deletion is possible
only through Barman, after an identical dry-run command succeeds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Protocol, Sequence

BACKUP_ID = re.compile(r"^[0-9]{8}T[0-9]{6}$")
PREFIX = re.compile(r"^[a-z0-9][a-z0-9._/-]*[a-z0-9]$")
BUDGET_BYTES = 4 * 1024**3
LEASE_MAX_AGE = timedelta(days=8)


@dataclass(frozen=True)
class Backup:
    backup_id: str
    status: str
    completed_at: datetime | None
    archival_marked: bool = False


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    kind: str
    backup_id: str | None = None


@dataclass(frozen=True)
class RestoreLease:
    result: str
    renew_time: datetime


@dataclass(frozen=True)
class RetentionState:
    prefix: str
    catalog_known: bool
    backups: tuple[Backup, ...]
    objects: tuple[StoredObject, ...]
    wal_archiver_stale: bool
    lease: RestoreLease | None
    validation_accepted: bool
    now: datetime
    budget_bytes: int = BUDGET_BYTES


@dataclass(frozen=True)
class RetentionPlan:
    backup_id: str | None = None
    blocked_reason: str | None = None
    catalog_fingerprint: str = ""
    total_base_bytes: int = 0

    @property
    def allowed(self) -> bool:
        return self.blocked_reason is None

    @property
    def needs_deletion(self) -> bool:
        return self.allowed and self.backup_id is not None


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str]) -> int: ...


class StateReader(Protocol):
    def read_state(self) -> RetentionState: ...


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _valid_prefix(prefix: str) -> bool:
    return bool(PREFIX.fullmatch(prefix)) and "//" not in prefix and not any(
        part in ("", ".", "..") for part in prefix.split("/")
    )


def _valid_backup_id(backup_id: str) -> bool:
    if not BACKUP_ID.fullmatch(backup_id):
        return False
    try:
        datetime.strptime(backup_id, "%Y%m%dT%H%M%S")
    except ValueError:
        return False
    return True


def _safe_object_key(key: str, prefix: str) -> bool:
    if not key.startswith(prefix + "/"):
        return False
    return not any(part in ("", ".", "..") for part in key.split("/"))


def _fingerprint(state: RetentionState) -> str:
    material = [state.prefix]
    material.extend(f"b:{b.backup_id}:{b.status}:{b.archival_marked}" for b in state.backups)
    material.extend(f"o:{o.key}:{o.size}:{o.kind}:{o.backup_id}" for o in state.objects)
    return hashlib.sha256("\n".join(sorted(material)).encode()).hexdigest()


def plan_retention(state: RetentionState) -> RetentionPlan:
    """Plan at most one oldest eligible Barman backup deletion."""
    if not _valid_prefix(state.prefix):
        return RetentionPlan(blocked_reason="foreign or manipulated prefix")
    if not state.catalog_known:
        return RetentionPlan(blocked_reason="unknown catalog")
    if state.budget_bytes < 0:
        return RetentionPlan(blocked_reason="invalid budget")
    if not _aware(state.now):
        return RetentionPlan(blocked_reason="ambiguous clock")
    if state.wal_archiver_stale:
        return RetentionPlan(blocked_reason="stale WAL archiver")
    if state.lease is None:
        return RetentionPlan(blocked_reason="missing restore lease")
    if state.lease.result != "success":
        return RetentionPlan(blocked_reason="failed restore lease")
    if not _aware(state.lease.renew_time):
        return RetentionPlan(blocked_reason="ambiguous restore lease time")
    lease_age = state.now - state.lease.renew_time
    if lease_age < timedelta(0) or lease_age > LEASE_MAX_AGE:
        return RetentionPlan(blocked_reason="stale restore lease")
    if not state.validation_accepted:
        return RetentionPlan(blocked_reason="isolated validation not accepted")

    by_id: dict[str, Backup] = {}
    completion_times: set[datetime] = set()
    for backup in state.backups:
        if not _valid_backup_id(backup.backup_id) or backup.backup_id in by_id:
            return RetentionPlan(blocked_reason="unknown catalog state")
        if backup.status == "running":
            return RetentionPlan(blocked_reason="backup running")
        if backup.status != "successful" or backup.completed_at is None or not _aware(backup.completed_at):
            return RetentionPlan(blocked_reason="failed or unknown catalog state")
        if backup.completed_at > state.now or backup.completed_at in completion_times:
            return RetentionPlan(blocked_reason="ambiguous catalog ordering")
        completion_times.add(backup.completed_at)
        by_id[backup.backup_id] = backup

    sizes = {backup_id: 0 for backup_id in by_id}
    seen_keys: set[str] = set()
    for obj in state.objects:
        if obj.key in seen_keys or obj.size < 0 or obj.kind not in {"base", "wal", "metadata"}:
            return RetentionPlan(blocked_reason="unknown object")
        seen_keys.add(obj.key)
        if not _safe_object_key(obj.key, state.prefix):
            return RetentionPlan(blocked_reason="foreign or manipulated prefix")
        relative = obj.key[len(state.prefix) + 1 :]
        if obj.kind == "base":
            if obj.backup_id not in by_id or not relative.startswith(f"base/{obj.backup_id}/"):
                return RetentionPlan(blocked_reason="unknown object/catalog mapping")
            sizes[obj.backup_id] += obj.size
        elif obj.backup_id is not None or not relative.startswith(obj.kind + "/"):
            return RetentionPlan(blocked_reason="unknown object/catalog mapping")

    if not by_id or any(size == 0 for size in sizes.values()):
        return RetentionPlan(blocked_reason="missing base-backup objects")
    total = sum(sizes.values())
    fingerprint = _fingerprint(state)
    if total <= state.budget_bytes:
        return RetentionPlan(catalog_fingerprint=fingerprint, total_base_bytes=total)

    newest = sorted(by_id.values(), key=lambda b: (b.completed_at, b.backup_id), reverse=True)
    if len(newest) <= 2:
        return RetentionPlan(blocked_reason="budget blocked by minimum redundancy", total_base_bytes=total)
    protected = {backup.backup_id for backup in newest[:2]}
    eligible = [b for b in newest if b.backup_id not in protected and not b.archival_marked]
    if not eligible:
        return RetentionPlan(blocked_reason="budget blocked by minimum redundancy or archival mark", total_base_bytes=total)
    oldest = min(eligible, key=lambda b: (b.completed_at, b.backup_id))
    return RetentionPlan(oldest.backup_id, catalog_fingerprint=fingerprint, total_base_bytes=total)


def barman_command(backup_id: str, *, dry_run: bool) -> tuple[str, ...]:
    command = ["barman-cloud-backup-delete"]
    if dry_run:
        command.append("--dry-run")
    command.extend(("--backup-id", backup_id, "--minimum-redundancy", "2"))
    return tuple(command)


def execute_plan(plan: RetentionPlan, reader: StateReader, runner: CommandRunner) -> RetentionPlan:
    """Revalidate, dry-run, delete through Barman, then reread full state."""
    if not plan.needs_deletion:
        return plan
    fresh = plan_retention(reader.read_state())
    if (
        not fresh.needs_deletion
        or fresh.backup_id != plan.backup_id
        or fresh.catalog_fingerprint != plan.catalog_fingerprint
    ):
        return RetentionPlan(blocked_reason="catalog changed before execution")
    assert plan.backup_id is not None
    if runner.run(barman_command(plan.backup_id, dry_run=True)) != 0:
        return RetentionPlan(blocked_reason="Barman dry-run failed")
    if runner.run(barman_command(plan.backup_id, dry_run=False)) != 0:
        return RetentionPlan(blocked_reason="Barman deletion failed")
    after = reader.read_state()
    if any(backup.backup_id == plan.backup_id for backup in after.backups):
        return RetentionPlan(blocked_reason="post-delete catalog reread did not confirm deletion")
    # Reading RetentionState necessarily includes catalog, base sizes and the full prefix.
    return plan_retention(after)


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def state_from_dict(raw: Mapping[str, object]) -> RetentionState:
    backups = tuple(
        Backup(
            backup_id=str(item["backup_id"]),
            status=str(item["status"]),
            completed_at=_datetime(item.get("completed_at")),
            archival_marked=bool(item.get("archival_marked", False)),
        )
        for item in raw.get("backups", [])  # type: ignore[union-attr]
    )
    objects = tuple(StoredObject(**item) for item in raw.get("objects", []))  # type: ignore[arg-type]
    lease_raw = raw.get("lease")
    lease = None
    if lease_raw:
        lease = RestoreLease(str(lease_raw["result"]), _datetime(lease_raw["renew_time"]))  # type: ignore[index,arg-type]
    return RetentionState(
        prefix=str(raw["prefix"]),
        catalog_known=bool(raw.get("catalog_known", False)),
        backups=backups,
        objects=objects,
        wal_archiver_stale=bool(raw.get("wal_archiver_stale", True)),
        lease=lease,
        validation_accepted=bool(raw.get("validation_accepted", False)),
        now=_datetime(str(raw["now"])),  # type: ignore[arg-type]
        budget_bytes=int(raw.get("budget_bytes", BUDGET_BYTES)),
    )


def _output(plan: RetentionPlan) -> dict[str, object]:
    return {
        "allowed": plan.allowed,
        "blocked_reason": plan.blocked_reason,
        "backup_id": plan.backup_id,
        "total_base_bytes": plan.total_base_bytes,
    }


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    reader: StateReader | None = None,
    runner: CommandRunner | None = None,
    stdout=sys.stdout,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", help="non-secret JSON state; stdin when omitted")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.execute:
        if reader is None or runner is None:
            parser.error("--execute requires injected state reader and command runner")
        plan = plan_retention(reader.read_state())
        result = execute_plan(plan, reader, runner)
    else:
        stream = open(args.state, encoding="utf-8") if args.state else sys.stdin
        try:
            result = plan_retention(state_from_dict(json.load(stream)))
        finally:
            if args.state:
                stream.close()
    json.dump(_output(result), stdout, sort_keys=True)
    stdout.write("\n")
    return 0 if result.allowed else 2


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
