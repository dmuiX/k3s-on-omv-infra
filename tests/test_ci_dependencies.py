"""Dependency preparation rejects corrupt caches and unsafe archive members."""

import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest


class DependencyIntegrity(unittest.TestCase):
    def test_cached_archives_are_verified_not_trusted_by_filename(self) -> None:
        from ci_dependencies import verify_file

        with tempfile.TemporaryDirectory(prefix="ci-dependency-") as directory:
            path = Path(directory) / "chart.tgz"
            path.write_bytes(b"public-fixture")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            verify_file(path, digest)
            path.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum"):
                verify_file(path, digest)

    def test_lock_must_match_the_applications_and_only_public_https(self) -> None:
        from ci_dependencies import validate_lock

        with tempfile.TemporaryDirectory(prefix="ci-dependency-") as directory:
            root = Path(directory)
            (root / "app.yml").write_text(
                "spec:\n  source:\n    chart: fixture\n    targetRevision: 1.2.3\n    repoURL: https://charts.example.invalid\n"
            )
            entry = {
                "chart": "fixture",
                "version": "1.2.3",
                "repository": "https://charts.example.invalid",
                "url": "https://github.com/example/charts/fixture.tgz",
                "sha256": "a" * 64,
            }
            validate_lock(root, {"charts": [entry]})
            entry["version"] = "9.9.9"
            with self.assertRaisesRegex(ValueError, "coordinates"):
                validate_lock(root, {"charts": [entry]})
            entry["version"] = "1.2.3"
            entry["url"] = "http://localhost/private"
            with self.assertRaisesRegex(ValueError, "public HTTPS"):
                validate_lock(root, {"charts": [entry]})

    def test_redirect_is_validated_before_the_next_http_request(self) -> None:
        from urllib.request import Request
        from ci_dependencies import PublicRedirects

        handler = PublicRedirects()
        request = Request("https://github.com/example/releases/tool")
        with self.assertRaisesRegex(ValueError, "public HTTPS"):
            handler.redirect_request(
                request, None, 302, "Found", {}, "http://localhost/private"
            )
        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://release-assets.githubusercontent.com/fixture",
        )
        self.assertIsNotNone(redirected)

    def test_tool_names_versions_and_checksums_are_closed(self) -> None:
        from ci_dependencies import validate_tools

        tools = {
            name: {
                "version": "v1.2.3",
                "url": "https://github.com/example/tool",
                "sha256": "a" * 64,
            }
            for name in ("helm", "argocd", "kustomize", "promtool")
        }
        validate_tools(tools)
        tools["../escaped"] = tools.pop("helm")
        with self.assertRaisesRegex(ValueError, "tool"):
            validate_tools(tools)
        tools["helm"] = tools.pop("../escaped")
        tools["helm"]["sha256"] = "invalid"
        with self.assertRaisesRegex(ValueError, "tool"):
            validate_tools(tools)

    def test_archive_cannot_write_outside_the_chart_directory(self) -> None:
        from ci_dependencies import extract_chart

        with tempfile.TemporaryDirectory(prefix="ci-dependency-") as directory:
            root = Path(directory)
            archive = root / "chart.tgz"
            with tarfile.open(archive, "w:gz") as output:
                member = tarfile.TarInfo("../../escaped")
                member.size = 1
                output.addfile(member, io.BytesIO(b"x"))
            with self.assertRaises((ValueError, tarfile.FilterError)):
                extract_chart(archive, root / "nested/charts")
            self.assertFalse((root / "escaped").exists())


if __name__ == "__main__":
    unittest.main()
