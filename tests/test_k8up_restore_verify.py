"""Offline restore contracts. The application-read tests use the CI-pinned promtool."""

from contextlib import redirect_stdout
import gzip
import importlib.util
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

TOOL = Path(__file__).resolve().parents[1] / "adapters/k8up-restore/verify.py"
spec = importlib.util.spec_from_file_location("restore_verify", TOOL)
assert spec and spec.loader
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)

SNAP = "20261015T120000Z-123abc"
BLOCK = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
MARKER = b"k8up-validation-canary:v1\n"


class RestoreTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.scratch = Path(tmp.name)
        self.restored = self.scratch / "restored"
        self.restored.mkdir()
        self.archive = self.restored / "kube-prometheus-stack-snapshottar"
        self.marker = self.restored / "data/k8up-validation-canary/marker.txt"
        (self.scratch / "snapshot-seconds").write_text("1710000100\n")

    def test_fixture_compares_known_bytes_not_a_restored_checksum(self) -> None:
        self.marker.parent.mkdir(parents=True)
        for bad in (b"", b"wrong\n", MARKER[:-1], MARKER + b"x"):
            with self.subTest(bad=bad):
                self.marker.write_bytes(bad)
                with self.assertRaises(ValueError):
                    verify.verify("backup-validation", self.scratch)
        self.marker.write_bytes(MARKER)
        with redirect_stdout(io.StringIO()) as log:
            verify.verify("backup-validation", self.scratch)
        self.assertEqual(log.getvalue(), "stage=backup-validation result=PASS\n")

    def test_fixture_does_not_follow_marker_or_ancestor_links(self) -> None:
        outside = self.scratch / "source"
        outside.mkdir()
        (outside / "marker.txt").write_bytes(MARKER)
        self.marker.parent.parent.mkdir()
        self.marker.parent.symlink_to(outside)
        with self.assertRaises(ValueError):
            verify.verify("backup-validation", self.scratch)
        self.marker.parent.unlink()
        self.marker.parent.mkdir()
        self.marker.symlink_to(outside / "marker.txt")
        with self.assertRaises(ValueError):
            verify.verify("backup-validation", self.scratch)
        self.marker.unlink()
        os.link(outside / "marker.txt", self.marker)
        with self.assertRaises(ValueError):
            verify.verify("backup-validation", self.scratch)

    def make_snapshot(self, member: tarfile.TarInfo | None = None) -> None:
        # Exactly snapshot.py's format (PAX fractional mtimes, single root, w|).
        root = self.scratch / SNAP
        chunk_dir = root / BLOCK / "chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        (chunk_dir / "000001").write_bytes(b"chunk")
        (chunk_dir.parent / "index").write_bytes(b"index")
        (chunk_dir.parent / "meta.json").write_bytes(b"{}")
        with self.archive.open("wb") as f, tarfile.open(fileobj=f, mode="w|") as tar:
            tar.add(root, arcname=root.name)
            if member is not None:
                tar.addfile(member, io.BytesIO(bytes(member.size)))

    def rejected(self) -> None:
        with self.assertRaises((ValueError, OSError, tarfile.TarError)):
            verify.verify("prometheus", self.scratch)
        self.assertFalse((self.scratch / "read-samples.sh").exists())

    def test_extracts_only_native_blocks_under_fresh_tsdb(self) -> None:
        self.make_snapshot()
        with redirect_stdout(io.StringIO()) as log:
            verify.verify("prometheus", self.scratch)
        self.assertEqual(log.getvalue(), "")  # structural validation isn't a PASS
        self.assertEqual((self.scratch / "tsdb" / BLOCK / "chunks/000001").read_bytes(), b"chunk")
        self.assertFalse((self.scratch / "read-samples.sh").exists())
        with self.assertRaises(FileExistsError):
            verify.verify("prometheus", self.scratch)

    def test_rejects_nested_special_files_and_ambiguous_members(self) -> None:
        chunk = f"{SNAP}/{BLOCK}/chunks/000002"
        for name, kind in [
            (chunk, tarfile.SYMTYPE), (chunk, tarfile.LNKTYPE),
            (chunk, tarfile.FIFOTYPE), (chunk, tarfile.CHRTYPE),
            (chunk, tarfile.BLKTYPE), (chunk, tarfile.GNUTYPE_SPARSE),
            (f"{SNAP}/{BLOCK}/../../outside", tarfile.REGTYPE),
            ("/outside", tarfile.REGTYPE), ("./outside", tarfile.REGTYPE),
            (f"{SNAP}/{BLOCK}/extra.tar", tarfile.REGTYPE),
            (f"{SNAP}/read-samples.sh", tarfile.REGTYPE),
            (f"{SNAP}/wal", tarfile.DIRTYPE),
            (f"{SNAP}/{BLOCK}/index", tarfile.REGTYPE),
            (SNAP + "B", tarfile.DIRTYPE),
        ]:
            with self.subTest(name=name, kind=kind):
                member = tarfile.TarInfo(name)
                member.type = kind
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    member.linkname = "../../outside"
                self.make_snapshot(member)
                self.rejected()
                self.assertFalse((self.scratch / "outside").exists())
                shutil.rmtree(self.scratch / "tsdb")

    def test_rejects_compressed_truncated_and_appended_inputs(self) -> None:
        self.make_snapshot()
        full = self.archive.read_bytes()
        for invalid in (gzip.compress(full), full[:1600], full + full):
            self.archive.write_bytes(invalid)
            self.rejected()
            shutil.rmtree(self.scratch / "tsdb")

    def test_rejects_pax_overrides_and_oversize_extended_header(self) -> None:
        member = tarfile.TarInfo(f"{SNAP}/{BLOCK}/chunks/000002")
        member.pax_headers = {"path": "../outside"}
        self.make_snapshot(member)
        self.rejected()
        shutil.rmtree(self.scratch / "tsdb")
        member = tarfile.TarInfo("././@PaxHeader")
        member.type = tarfile.XHDTYPE
        member.size = 4 * 1024**3
        self.archive.write_bytes(member.tobuf())
        self.rejected()

    def test_rejects_wrong_input_tree_or_linked_destination(self) -> None:
        self.make_snapshot()
        wrong = self.restored / "kube-prometheus-stack-snapshot.tar"
        self.archive.rename(wrong)
        self.rejected()
        self.archive.symlink_to(wrong)
        wrong.rename(self.scratch / "elsewhere")
        self.rejected()
        self.archive.unlink()
        (self.scratch / "elsewhere").rename(self.archive)
        os.mkfifo(wrong)
        self.rejected()
        wrong.unlink()
        (self.scratch / "tsdb").symlink_to(self.restored)
        self.rejected()

    def test_scratch_and_member_limits_fail_closed(self) -> None:
        self.make_snapshot()
        with patch.object(verify, "MAX_BYTES", self.archive.stat().st_size - 1):
            self.rejected()
        with patch.object(verify, "GIB", shutil.disk_usage(self.scratch).total):
            self.rejected()
        with patch.object(verify, "MAX_MEMBERS", 2):
            self.rejected()

    def read_samples(self, promtool: str) -> subprocess.CompletedProcess[bytes]:
        # Substitute only the two container-local absolute paths for this host.
        script = (
            TOOL.with_name("read-samples.sh").read_text()
            .replace("/scratch", shlex.quote(str(self.scratch)))
            .replace("/bin/promtool", shlex.quote(promtool))
        )
        return subprocess.run(["/bin/sh", "-c", script], capture_output=True, timeout=60)

    def test_sample_then_late_error_is_not_a_pass(self) -> None:
        self.make_snapshot()
        verify.verify("prometheus", self.scratch)
        tool = self.scratch / "fixture-command"
        for status in (0, 1):
            tool.write_text(
                '#!/bin/sh\necho \'{__name__="kube_node_info", node="private_fixture_label"} 1 1710000000000\'\n'
                f"echo 'private diagnostic' >&2\nexit {status}\n"
            )
            tool.chmod(0o700)
            result = self.read_samples(str(tool))
            self.assertEqual(result.returncode == 0, status == 0)
            self.assert_result(result, status == 0)

    def assert_result(self, result, passed):
        self.assertEqual(result.returncode == 0, passed)
        if passed:
            self.assertEqual((result.stdout, result.stderr), (b"stage=read-samples result=PASS\n", b""))
        else:
            self.assertEqual((result.stdout, result.stderr), (b"", b"stage=read-samples result=FAIL\n"))

    def test_application_reads_actual_samples_not_just_metadata(self) -> None:
        # No server, Docker, repository or network. CI provisions promtool v3.15.0.
        promtool = shutil.which("promtool")
        self.assertIsNotNone(promtool, "offline fixture requires the CI-pinned promtool in PATH")
        assert promtool is not None
        for series, node, value, time, passed in [
            ("kube_node_info", "node-a", 1, 1710000000, True),
            ("up", "node-a", 1, 1710000000, False),
            ("kube_node_info", "", 1, 1710000000, False),
            ("kube_node_info", "node-a", 0, 1710000000, False),
            ("kube_node_info", "node-a", 1, 1709900000, False),
            ("kube_node_info", "node-a", 1, 1710000200, False),
        ]:
            with self.subTest(series=series, node=node, value=value, time=time):
                om = self.scratch / "test.om"
                om.write_text(f'{series}{{node="{node}",job="private_fixture_label"}} {value} {time}\n# EOF\n')
                root = self.scratch / SNAP
                subprocess.run([promtool, "tsdb", "create-blocks-from", "openmetrics", str(om), str(root)],
                               capture_output=True, check=True, timeout=60)
                with self.archive.open("wb") as f, tarfile.open(fileobj=f, mode="w|") as tar:
                    tar.add(root, arcname=root.name)
                verify.verify("prometheus", self.scratch)
                result = self.read_samples(promtool)
                self.assert_result(result, passed)
                if passed:
                    (chunk,) = (self.scratch / "tsdb").glob("*/chunks/000001")
                    chunk.write_bytes(b"unreadable chunk")
                    result = self.read_samples(promtool)
                    self.assert_result(result, False)
                shutil.rmtree(self.scratch / "tsdb")
                shutil.rmtree(root)
