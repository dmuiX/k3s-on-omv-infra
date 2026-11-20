"""Pinned public-only CI tools/charts; no credentials or cluster access."""

import argparse
import bz2
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml

PUBLIC_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "raw.githubusercontent.com",
    "get.helm.sh",
    "blob.istio.io",
    "istio-release.storage.googleapis.com",
    "istio-release.r2.istio.io",
    "charts.jetstack.io",
    "charts.crossplane.io",
    "helm.releases.hashicorp.com",
}
COORDINATE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
VERSION = re.compile(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")


def verify_file(path: Path, expected: str) -> None:
    """Reject corrupted or unverified archives, including restored CI caches."""
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None or path.is_symlink():
        raise ValueError("Invalid checksum or redirected dependency file")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError("Dependency checksum mismatch: " + path.name)


def extract_chart(archive: Path, destination: Path) -> None:
    """Extract only safe archive data into a fresh, non-cache render directory."""
    if destination.exists() or destination.is_symlink():
        raise ValueError("Chart extraction needs a fresh destination")
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if sum(m.size for m in members) > 128 * 1024 * 1024:
            raise ValueError("Chart archive exceeds the extraction limit")
        # Reject the entire archive before writing anything on path/link failures.
        for member in members:
            tarfile.data_filter(member, str(destination))
        destination.mkdir(parents=True)
        source.extractall(destination, members=members, filter="data")


def public_url(url: str) -> None:
    """Accept only credential-free HTTPS dependency origins, never private URLs."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in PUBLIC_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Dependency must use an approved public HTTPS origin")


class PublicRedirects(HTTPRedirectHandler):
    """Reject non-public redirect targets before urllib makes another request."""

    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Request | None:
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_tools(tools: dict[str, Any]) -> None:
    """Close executable names and integrity/version inputs before writing files."""
    if set(tools) != {"helm", "argocd", "kustomize", "promtool", "restic"}:
        raise ValueError("Invalid tool names")
    for tool in tools.values():
        if (
            not VERSION.fullmatch(tool["version"])
            or re.fullmatch(r"[0-9a-f]{64}", tool["sha256"]) is None
        ):
            raise ValueError("Invalid tool version or checksum")
        public_url(tool["url"])
        if "member" in tool:
            member = Path(tool["member"])
            if member.is_absolute() or ".." in member.parts:
                raise ValueError("Invalid tool archive member")


def validate_lock(root: Path, lock: dict[str, Any]) -> list[dict[str, str]]:
    """Every remote Helm coordinate must have exactly one integrity pin."""
    expected = set()
    for path in root.rglob("app.yml"):
        app = yaml.safe_load(path.read_text())
        for source in app["spec"].get("sources", [app["spec"].get("source", {})]):
            if "chart" in source:
                expected.add(
                    (source["repoURL"], source["chart"], str(source["targetRevision"]))
                )
    records: list[dict[str, str]] = lock["charts"]
    actual = set()
    for entry in records:
        if not COORDINATE.fullmatch(entry["chart"]) or not VERSION.fullmatch(
            entry["version"]
        ):
            raise ValueError("Invalid locked chart coordinates")
        if re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None:
            raise ValueError("Invalid locked chart checksum")
        public_url(entry["url"])
        actual.add((entry["repository"], entry["chart"], entry["version"]))
    if actual != expected or len(actual) != len(records):
        raise ValueError("Dependency lock coordinates do not match Applications")
    return records


def download(entry: dict[str, str], destination: Path) -> None:
    """Verify cache hits; otherwise download an anonymous, bounded public asset."""
    public_url(entry["url"])
    if destination.exists() or destination.is_symlink():
        verify_file(destination, entry["sha256"])
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            request = Request(entry["url"], headers={"User-Agent": "Helm/3.19.0"})
            with build_opener(PublicRedirects()).open(request, timeout=30) as response:
                public_url(response.url)
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > 256 * 1024 * 1024:
                        raise ValueError("Dependency download exceeds size limit")
                    output.write(chunk)
            output.flush()
            verify_file(temporary, entry["sha256"])
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> int:
    """Prepare verified archives and fresh renders without reading private trees."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--charts", type=Path, required=True)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument(
        "--only", help="Prepare one chart/tool (developer feedback loop)"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    context = "lock"
    try:
        lock = json.loads((root / "tests/ci-dependencies.json").read_text())
        entries = validate_lock(root, lock)
        validate_tools(lock["tools"])
        if args.only and args.only not in set(lock["tools"]) | {
            e["chart"] for e in entries
        }:
            raise ValueError("Unknown dependency selection")
        args.bin.mkdir(parents=True, exist_ok=True)
        for name, tool in lock["tools"].items():
            if args.only and args.only != name:
                continue
            context = name + "-" + tool["version"]
            archive = args.cache / context
            download(tool, archive)
            binary = args.bin / name
            if binary.is_symlink():
                raise ValueError("Refusing a redirected tool destination")
            if "member" in tool:
                with tarfile.open(archive, "r:gz") as source:
                    member = source.getmember(tool["member"])
                    if not member.isfile():
                        raise ValueError("Tool member is not a regular file")
                    stream = source.extractfile(member)
                    assert stream is not None
                    binary.write_bytes(stream.read())
            elif tool["url"].endswith(".bz2"):
                with bz2.open(archive, "rb") as compressed, binary.open("wb") as output:
                    shutil.copyfileobj(compressed, output)
            else:
                binary.write_bytes(archive.read_bytes())
            binary.chmod(0o755)
            print("Verified tool: " + name + " " + tool["version"])
        for entry in entries:
            if args.only and args.only != entry["chart"]:
                continue
            name = entry["chart"] + "-" + entry["version"]
            context = name
            archive = args.cache / (name + ".tgz")
            download(entry, archive)
            destination = args.charts / name
            extract_chart(archive, destination)
            metadata = yaml.safe_load(
                (destination / entry["chart"] / "Chart.yaml").read_text()
            )
            if (
                metadata["name"] != entry["chart"]
                or str(metadata["version"]) != entry["version"]
            ):
                raise ValueError("Chart metadata does not match the locked coordinate")
            print("Verified chart: " + name)
    except Exception as error:
        # Never dump HTTP responses, environment, signed redirects or chart data.
        print(
            "CI dependency preparation failed for "
            + context
            + " ("
            + type(error).__name__
            + "); check the pinned source/cache"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
