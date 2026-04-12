"""Tests for skillsyncer.hooks."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from skillsyncer.hooks import (
    START_MARKER,
    hook_is_installed,
    install_hooks,
    uninstall_hooks,
)


def _git_init(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_install_into_fresh_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    written = install_hooks(repo)
    assert len(written) == 2
    pre = repo / ".git" / "hooks" / "pre-push"
    assert pre.exists()
    assert START_MARKER in pre.read_text()
    if sys.platform != "win32":
        # os.access(..., X_OK) is meaningless on Windows; chmod is best-effort.
        assert os.access(pre, os.X_OK)
    assert hook_is_installed(repo, "pre-push")
    assert hook_is_installed(repo, "post-merge")


def test_install_is_idempotent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    install_hooks(repo)
    first = (repo / ".git" / "hooks" / "pre-push").read_text()
    install_hooks(repo)
    second = (repo / ".git" / "hooks" / "pre-push").read_text()
    assert first == second
    # Marker block must appear exactly once.
    assert second.count(START_MARKER) == 1


def test_install_preserves_existing_hook_content(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    pre = repo / ".git" / "hooks" / "pre-push"
    pre.write_text("#!/bin/bash\necho 'user hook'\nexit 0\n")
    install_hooks(repo)
    text = pre.read_text()
    assert "echo 'user hook'" in text
    assert START_MARKER in text


def test_uninstall_removes_block(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    pre = repo / ".git" / "hooks" / "pre-push"
    pre.write_text("#!/bin/bash\necho 'user hook'\nexit 0\n")
    install_hooks(repo)
    uninstall_hooks(repo)
    text = pre.read_text()
    assert START_MARKER not in text
    assert "echo 'user hook'" in text


def test_uninstall_removes_file_when_only_skillsyncer(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    install_hooks(repo)
    uninstall_hooks(repo)
    assert not (repo / ".git" / "hooks" / "pre-push").exists()
    assert not (repo / ".git" / "hooks" / "post-merge").exists()


def test_install_raises_outside_git_repo(tmp_path):
    with pytest.raises(FileNotFoundError):
        install_hooks(tmp_path / "not-a-repo")
