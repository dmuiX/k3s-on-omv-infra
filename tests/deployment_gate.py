#!/usr/bin/env python3
"""Fail closed while any machine-readable Infra deployment blocker is open."""
import json
import sys
from pathlib import Path

registry_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "deployment-blockers.json"
registry = json.loads(registry_path.read_text())
open_blockers = [b["id"] for b in registry["blockers"] if b["status"] != "resolved" and b["blocksDeployment"]]
if open_blockers or not registry.get("deploymentAllowed", False):
    print("deployment blocked: " + ", ".join(open_blockers), file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(0)
