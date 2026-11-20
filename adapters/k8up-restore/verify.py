"""Offline verification on exclusive scratch. Never execute or extract metadata.

The immutable snapshot producer uses Python tarfile.add (plain tar, numeric
local PAX records for e.g. fractional mtimes). TarFile iteration processes
extended/sparse headers *before* yielding a member to an extraction filter.
Use stdlib TarInfo.frombuf on individual fixed-size headers instead: bound PAX
before reading it, and stream only allowlisted regular-file bytes to fresh paths.
"""
import os
import re
import shutil
import stat
import sys
import tarfile
from pathlib import Path
from typing import BinaryIO

GIB = 1024**3
MAX_BYTES = 20 * GIB
MAX_MEMBERS = 32_768
MARKER = b"k8up-validation-canary:v1\n"
ARCHIVE = "kube-prometheus-stack-snapshottar"
SNAPSHOT = re.compile(r"[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]{1,64}")
BLOCK = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
DIRECTORY = re.compile(rf"{BLOCK}(/chunks)?")
FILE = re.compile(rf"{BLOCK}/(meta\.json|index|tombstones|chunks/[0-9]{{6}})")


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid restore")


def open_regular(path: Path) -> BinaryIO:
    st = path.lstat()
    require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1)
    return os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb")


def padding(data: BinaryIO, size: int) -> None:
    count = -size % 512
    require(data.read(count) == bytes(count))


def pax_size(data: BinaryIO, length: int) -> int | None:
    require(0 < length <= 4096)
    payload = data.read(length)
    require(len(payload) == length)
    padding(data, length)
    size = None
    for line in payload.splitlines(keepends=True):
        m = re.fullmatch(rb"([1-9][0-9]{0,3}) (mtime|uid|gid|size)=(-?[0-9]+(?:\.[0-9]+)?)\n", line)
        if not m or int(m[1]) != len(line):
            raise ValueError("invalid PAX record")
        if m[2] == b"size":
            require(size is None and m[3].isdigit())
            size = int(m[3])
    return size


def copy_payload(data: BinaryIO, path: Path, size: int) -> None:
    with path.open("xb") as out:  # no duplicate files or implicit parents
        remaining = size
        while remaining:
            chunk = data.read(min(remaining, 1024**2))
            require(bool(chunk))
            out.write(chunk)
            remaining -= len(chunk)
    padding(data, size)


def extract(data: BinaryIO, dest: Path) -> None:
    root = None
    for index in range(MAX_MEMBERS + 1):
        raw = data.read(512)
        if raw == bytes(512):  # first zero block; reject a hidden second archive
            tail = data.read(tarfile.RECORDSIZE + 513)
            require(512 <= len(tail) <= tarfile.RECORDSIZE + 512 and len(tail) % 512 == 0 and not any(tail))
            return
        require(index < MAX_MEMBERS)
        member = tarfile.TarInfo.frombuf(raw, "utf-8", "strict")
        if member.type == tarfile.XHDTYPE:
            size = pax_size(data, member.size)
            member = tarfile.TarInfo.frombuf(data.read(512), "utf-8", "strict")
            if size is not None:
                member.size = size
        require(not member.linkname and 0 <= member.size <= MAX_BYTES)
        directory = member.type == tarfile.DIRTYPE
        require((directory and member.size == 0) or member.type == tarfile.REGTYPE)
        if root is None:
            require(directory and SNAPSHOT.fullmatch(member.name) is not None)
            root = member.name
            continue
        snapshot, _, relative = member.name.partition("/")
        require(snapshot == root and (DIRECTORY if directory else FILE).fullmatch(relative) is not None)
        if directory:
            (dest / relative).mkdir()
        else:
            # No compression, sparse files or links: total output cannot exceed
            # the already capped input file size. Truncated payloads fail closed.
            copy_payload(data, dest / relative, member.size)


def verify(mode: str, scratch: Path) -> None:
    restored = scratch / "restored"
    require(stat.S_ISDIR(restored.lstat().st_mode))
    if mode == "canary":
        for name in ("data", "k8up-validation-canary"):
            restored = restored / name
            require(stat.S_ISDIR(restored.lstat().st_mode))
        with open_regular(restored / "marker.txt") as marker:
            require(marker.read(len(MARKER) + 1) == MARKER)
        print("stage=canary result=PASS")
        return
    require(mode == "prometheus")
    with os.scandir(restored) as entries:
        entry = next(entries, None)
        require(entry is not None and entry.name == ARCHIVE and next(entries, None) is None)
    dest = scratch / "tsdb"
    with open_regular(restored / ARCHIVE) as data:
        size = os.fstat(data.fileno()).st_size
        require(0 < size <= MAX_BYTES and shutil.disk_usage(scratch).free >= size + GIB)
        dest.mkdir()  # never merge with old output
        extract(data, dest)
    require(any(dest.iterdir()))
    for block in dest.iterdir():
        require((block / "meta.json").stat().st_size > 0 and (block / "index").stat().st_size > 0)
        require(any((block / "chunks").iterdir()))
    # promtool v3.15 requires a WAL directory even for immutable blocks. Never
    # accept one from the archive; create it empty after checking all blocks.
    (dest / "wal").mkdir()
    # This stage is not a PASS. Only the final promtool container can report it.


if __name__ == "__main__":
    try:
        require(len(sys.argv) == 2)
        verify(sys.argv[1], Path("/scratch"))
    except (OSError, ValueError, tarfile.TarError):
        sys.exit("stage=validation result=FAIL")  # never print restored names, labels or contents
