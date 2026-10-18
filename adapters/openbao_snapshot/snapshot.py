"""Fail-closed count retention for verified OpenBao snapshot uploads.

Planning is pure.  The executor accepts an object client so credentials remain in
the surrounding runtime and never enter arguments, plans, or logs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, Sequence

SNAPSHOT_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
PREFIX = re.compile(r"^[a-z0-9][a-z0-9._/-]*[a-z0-9]$")
RETENTION = {"daily": 7, "weekly": 4, "monthly": 6}


@dataclass(frozen=True)
class Snapshot:
    prefix: str
    snapshot_id: str
    snapshot_key: str
    checksum_key: str
    upload_succeeded: bool
    checksum_verified: bool


@dataclass(frozen=True)
class SnapshotState:
    prefixes: Mapping[str, str]
    snapshots: tuple[Snapshot, ...]
    listed_keys: tuple[str, ...]
    current_snapshot_id: str
    required_uploads: tuple[str, ...]


@dataclass(frozen=True)
class RetentionPlan:
    deletions: tuple[str, ...] = ()
    blocked_reason: str | None = None
    fingerprint: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.blocked_reason is None


class ObjectClient(Protocol):
    def read_state(self) -> SnapshotState: ...
    def delete_object(self, key: str) -> None: ...


def _valid_prefix(prefix: str) -> bool:
    return bool(PREFIX.fullmatch(prefix)) and "//" not in prefix and not any(
        part in ("", ".", "..") for part in prefix.split("/")
    )


def _valid_snapshot_id(snapshot_id: str) -> bool:
    if not SNAPSHOT_ID.fullmatch(snapshot_id):
        return False
    try:
        datetime.strptime(snapshot_id, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return False
    return True


def _expected_keys(prefix: str, snapshot_id: str) -> tuple[str, str]:
    stem = f"{prefix}/{snapshot_id}"
    return f"{stem}.snap", f"{stem}.snap.sha256"


def plan_retention(state: SnapshotState) -> RetentionPlan:
    """Return an atomic deletion plan, or no plan for every ambiguity."""
    if set(state.prefixes) != set(RETENTION):
        return RetentionPlan(blocked_reason="unknown prefix configuration")
    if len(set(state.prefixes.values())) != len(RETENTION) or any(
        not _valid_prefix(value) for value in state.prefixes.values()
    ):
        return RetentionPlan(blocked_reason="foreign or manipulated prefix")
    if not _valid_snapshot_id(state.current_snapshot_id):
        return RetentionPlan(blocked_reason="invalid current snapshot id")
    if (
        not state.required_uploads
        or len(set(state.required_uploads)) != len(state.required_uploads)
        or not set(state.required_uploads) <= set(RETENTION)
    ):
        return RetentionPlan(blocked_reason="unknown required upload set")

    records: dict[tuple[str, str], Snapshot] = {}
    known_keys: set[str] = set()
    for item in state.snapshots:
        if item.prefix not in RETENTION or not _valid_snapshot_id(item.snapshot_id):
            return RetentionPlan(blocked_reason="unknown snapshot object")
        configured = state.prefixes[item.prefix]
        expected = _expected_keys(configured, item.snapshot_id)
        if (item.snapshot_key, item.checksum_key) != expected:
            return RetentionPlan(blocked_reason="foreign or manipulated prefix")
        identity = (item.prefix, item.snapshot_id)
        if identity in records or item.snapshot_key in known_keys or item.checksum_key in known_keys:
            return RetentionPlan(blocked_reason="duplicate snapshot object")
        if not item.upload_succeeded or not item.checksum_verified:
            return RetentionPlan(blocked_reason="failed or partial upload")
        records[identity] = item
        known_keys.update(expected)

    listed = set(state.listed_keys)
    if len(listed) != len(state.listed_keys) or listed != known_keys:
        return RetentionPlan(blocked_reason="unknown or missing snapshot object")
    for prefix in state.required_uploads:
        current = records.get((prefix, state.current_snapshot_id))
        if current is None or not current.upload_succeeded or not current.checksum_verified:
            return RetentionPlan(blocked_reason="failed or partial current upload")
        prefix_ids = [snapshot_id for kind, snapshot_id in records if kind == prefix]
        if state.current_snapshot_id != max(prefix_ids):
            return RetentionPlan(blocked_reason="ambiguous current upload ordering")

    deletions: list[str] = []
    for prefix, keep in RETENTION.items():
        items = sorted(
            (item for (kind, _), item in records.items() if kind == prefix),
            key=lambda item: item.snapshot_id,
            reverse=True,
        )
        for item in items[keep:]:
            deletions.extend((item.snapshot_key, item.checksum_key))
    return RetentionPlan(tuple(deletions), fingerprint=tuple(sorted(known_keys)))


def execute_plan(plan: RetentionPlan, client: ObjectClient) -> RetentionPlan:
    """Revalidate immediately before deleting only the planned exact keys."""
    if not plan.allowed:
        return plan
    fresh = plan_retention(client.read_state())
    if not fresh.allowed or fresh.deletions != plan.deletions or fresh.fingerprint != plan.fingerprint:
        return RetentionPlan(blocked_reason="state changed before execution")
    for key in plan.deletions:
        client.delete_object(key)
    return plan


def state_from_dict(raw: Mapping[str, object]) -> SnapshotState:
    snapshots = tuple(Snapshot(**item) for item in raw.get("snapshots", []))  # type: ignore[arg-type]
    return SnapshotState(
        prefixes=dict(raw["prefixes"]),  # type: ignore[arg-type]
        snapshots=snapshots,
        listed_keys=tuple(raw.get("listed_keys", [])),  # type: ignore[arg-type]
        current_snapshot_id=str(raw["current_snapshot_id"]),
        required_uploads=tuple(raw.get("required_uploads", [])),  # type: ignore[arg-type]
    )


def _output(plan: RetentionPlan) -> dict[str, object]:
    return {"allowed": plan.allowed, "blocked_reason": plan.blocked_reason, "deletions": list(plan.deletions)}


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    client: ObjectClient | None = None,
    stdout=sys.stdout,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", help="non-secret JSON state; stdin when omitted")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.execute:
        if client is None:
            parser.error("--execute requires an injected object client")
        plan = plan_retention(client.read_state())
        result = execute_plan(plan, client)
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
