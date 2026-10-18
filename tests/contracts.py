"""Standard-library helpers for SPEC v6.1 Infra structural tests."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = {
    "argocd-config": ("01-bootstrap/argocd-config", 1),
    "prometheus-operator-crds": ("01-bootstrap/prometheus-operator-crds", 1),
    "barman-cloud": ("02-controllers/barman-cloud", 2),
    "cert-manager": ("02-controllers/cert-manager", 2),
    "cloudnative-pg": ("02-controllers/cloudnative-pg", 2),
    "crossplane": ("02-controllers/crossplane", 2),
    "istio-ambient": ("02-controllers/istio-ambient", 2),
    "k8up": ("02-controllers/k8up", 2),
    "longhorn": ("02-controllers/longhorn", 2),
    "vault-secrets-operator": ("02-controllers/vault-secrets-operator", 2),
    "provider-vault": ("03-controllers/provider-vault", 3),
    "openbao": ("04-core/openbao", 4),
    "provider-vault-connection": ("05-platform/provider-vault-connection", 5),
    "letsencrypt-dns01": ("06-platform/letsencrypt-dns01", 6),
    "openbao-backups": ("06-platform/openbao-backups", 6),
    "openbao-config": ("06-platform/openbao-config", 6),
    "openbao-pki": ("06-platform/openbao-pki", 6),
    "postgresql": ("07-data/postgresql", 7),
    "backup-validation": ("08-observability/backup-validation", 8),
    "fluent-bit": ("08-observability/fluent-bit", 8),
    "kube-prometheus-stack": ("08-observability/kube-prometheus-stack", 8),
}


def app_files() -> list[Path]:
    return sorted(ROOT.glob("0[1-8]-*/*/app.yml"))


def text(path: str | Path) -> str:
    return (ROOT / path).read_text(encoding="utf-8") if not isinstance(path, Path) or not path.is_absolute() else path.read_text(encoding="utf-8")


def app_name(content: str) -> str:
    match = re.search(r"(?m)^  name: ([a-z0-9-]+)$", content)
    if not match:
        raise AssertionError("Application has no canonical metadata.name")
    return match.group(1)


def app_wave(content: str) -> int:
    match = re.search(r'argocd\.argoproj\.io/sync-wave: ["\']?(-?\d+)', content)
    if not match:
        raise AssertionError("Application has no sync wave")
    return int(match.group(1))


def blockers() -> dict:
    return json.loads(text("deployment-blockers.json"))


def blocker_annotations(content: str) -> list[str]:
    return re.findall(r"infra\.k3s-on-omv\.dev/deployment-blocker:\s*([A-Z0-9_]+)", content)
