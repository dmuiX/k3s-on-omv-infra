"""Seed only the dedicated canary PVC, without modifying existing entries."""
import os
import stat
from pathlib import Path


MARKER = b"k8up-validation-canary:v1\n"


def seed(directory: Path) -> None:
    path = directory / "marker.txt"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        # Don't follow a link, block on a FIFO, or read an unbounded file.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as existing:
            st = os.fstat(existing.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or
                    existing.read(len(MARKER) + 1) != MARKER):
                raise ValueError("existing canary is not the expected regular file")
    else:
        with os.fdopen(fd, "wb") as new:
            new.write(MARKER)
            new.flush()
            os.fsync(new.fileno())


if __name__ == "__main__":
    seed(Path("/canary"))
