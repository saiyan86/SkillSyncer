"""Shared filesystem helpers."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def atomic_write(path: Path, data: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".skillsyncer-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_copy(src: Path, dst: Path) -> None:
    """Copy ``src`` byte-for-byte to ``dst`` via a tmp+rename in the
    destination dir, so partial copies are never visible at ``dst``."""
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=".skillsyncer-tmp-")
    os.close(fd)
    try:
        shutil.copyfile(str(src), tmp)
        os.replace(tmp, str(dst))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
