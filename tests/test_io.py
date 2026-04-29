"""Tests for skillsyncer._io."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from skillsyncer._io import atomic_write


def test_writes_new_file(tmp_path):
    p = tmp_path / "out.txt"
    atomic_write(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"


def test_overwrites_existing_file(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("old content")
    atomic_write(p, "new content")
    assert p.read_text(encoding="utf-8") == "new content"


def test_creates_missing_parent_dirs(tmp_path):
    p = tmp_path / "a" / "b" / "c" / "out.txt"
    atomic_write(p, "deep")
    assert p.read_text(encoding="utf-8") == "deep"


def test_accepts_string_path(tmp_path):
    p = tmp_path / "out.txt"
    atomic_write(str(p), "via str")
    assert p.read_text(encoding="utf-8") == "via str"


def test_writes_unicode(tmp_path):
    p = tmp_path / "out.txt"
    atomic_write(p, "héllo — 世界 — 🚀")
    assert p.read_text(encoding="utf-8") == "héllo — 世界 — 🚀"


def test_writes_empty_string(tmp_path):
    p = tmp_path / "empty.txt"
    atomic_write(p, "")
    assert p.exists()
    assert p.read_text(encoding="utf-8") == ""


def test_no_temp_file_left_on_success(tmp_path):
    p = tmp_path / "out.txt"
    atomic_write(p, "ok")
    leftovers = [c for c in tmp_path.iterdir() if c.name.startswith(".skillsyncer-tmp-")]
    assert leftovers == []


def test_temp_file_cleaned_up_on_failure(tmp_path):
    p = tmp_path / "out.txt"
    # Force os.replace to fail after the temp file is written.
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated failure")

    with patch("skillsyncer._io.os.replace", side_effect=boom):
        with pytest.raises(OSError, match="simulated failure"):
            atomic_write(p, "data")

    # Both targets must be absent: the original was never created, and the
    # temp file must have been unlinked by the cleanup branch.
    assert not p.exists()
    leftovers = [c for c in tmp_path.iterdir() if c.name.startswith(".skillsyncer-tmp-")]
    assert leftovers == []


def test_existing_file_unchanged_on_failure(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("original")

    def boom(src, dst):
        raise OSError("nope")

    with patch("skillsyncer._io.os.replace", side_effect=boom):
        with pytest.raises(OSError):
            atomic_write(p, "would-be-new")

    # The atomicity guarantee: a failed write must leave the prior file intact.
    assert p.read_text(encoding="utf-8") == "original"


def test_temp_file_is_in_target_directory(tmp_path):
    """The temp file must live alongside the target so os.replace stays atomic.

    Atomic rename requires same-filesystem source and destination; if the
    temp went to /tmp, a cross-device rename could silently fall back to
    copy+unlink and break atomicity.
    """
    p = tmp_path / "out.txt"
    seen_dirs: list[str] = []
    real_mkstemp = __import__("tempfile").mkstemp

    def spy_mkstemp(*args, **kwargs):
        seen_dirs.append(kwargs.get("dir") or (args[2] if len(args) > 2 else None))
        return real_mkstemp(*args, **kwargs)

    with patch("skillsyncer._io.tempfile.mkstemp", side_effect=spy_mkstemp):
        atomic_write(p, "x")

    assert seen_dirs and Path(seen_dirs[0]).resolve() == tmp_path.resolve()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only mode check")
def test_replace_preserves_target_when_pre_existing(tmp_path):
    """os.replace must overwrite atomically — no window where the file is missing."""
    p = tmp_path / "out.txt"
    p.write_text("v1")
    atomic_write(p, "v2")
    assert p.exists()
    assert p.read_text(encoding="utf-8") == "v2"
