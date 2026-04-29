"""Tests for skillsyncer.paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from skillsyncer import paths


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Each test starts with SKILLSYNCER_HOME unset so the default branch is exercised."""
    monkeypatch.delenv("SKILLSYNCER_HOME", raising=False)


def test_home_default_uses_user_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.home() == tmp_path / ".skillsyncer"


def test_home_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom-home"
    monkeypatch.setenv("SKILLSYNCER_HOME", str(custom))
    assert paths.home() == custom


def test_home_env_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SKILLSYNCER_HOME", "~/elsewhere")
    assert paths.home() == tmp_path / "elsewhere"


def test_home_empty_env_falls_back_to_default(monkeypatch, tmp_path):
    """An empty string is falsy, so the default branch must run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SKILLSYNCER_HOME", "")
    assert paths.home() == tmp_path / ".skillsyncer"


def test_subpaths_compose_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLSYNCER_HOME", str(tmp_path))
    assert paths.identity_path() == tmp_path / "identity.yaml"
    assert paths.config_path() == tmp_path / "config.yaml"
    assert paths.state_path() == tmp_path / "state.yaml"
    assert paths.reports_dir() == tmp_path / "reports"
    assert paths.repos_dir() == tmp_path / "repos"


def test_each_call_re_reads_env(monkeypatch, tmp_path):
    """home() must read the env each call, not cache it — important for tests."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    monkeypatch.setenv("SKILLSYNCER_HOME", str(a))
    assert paths.home() == a
    monkeypatch.setenv("SKILLSYNCER_HOME", str(b))
    assert paths.home() == b


def test_subpaths_track_env_changes(monkeypatch, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    monkeypatch.setenv("SKILLSYNCER_HOME", str(a))
    first = paths.config_path()
    monkeypatch.setenv("SKILLSYNCER_HOME", str(b))
    second = paths.config_path()
    assert first.parent == a
    assert second.parent == b


def test_returned_paths_are_pathlib_objects(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLSYNCER_HOME", str(tmp_path))
    for fn in (
        paths.home,
        paths.identity_path,
        paths.config_path,
        paths.state_path,
        paths.reports_dir,
        paths.repos_dir,
    ):
        assert isinstance(fn(), Path)
