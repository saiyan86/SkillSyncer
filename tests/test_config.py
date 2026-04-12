"""Tests for skillsyncer.config."""

from __future__ import annotations

from skillsyncer.config import add_source, read_config, write_config, detect_targets


def test_read_missing_returns_empty(tmp_path):
    p = tmp_path / "c.yaml"
    assert read_config(p) == {"sources": [], "targets": []}


def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "c.yaml"
    cfg = {
        "sources": [{"name": "main", "url": "git@github.com:me/skills.git"}],
        "targets": [{"name": "claude-code", "path": "~/.claude/skills"}],
    }
    write_config(cfg, p)
    out = read_config(p)
    assert out["sources"][0]["name"] == "main"
    assert out["targets"][0]["path"] == "~/.claude/skills"


def test_add_source_appends(tmp_path):
    p = tmp_path / "c.yaml"
    add_source("https://github.com/a/b", "ab", p)
    add_source("https://github.com/c/d", "cd", p)
    cfg = read_config(p)
    names = [s["name"] for s in cfg["sources"]]
    assert names == ["ab", "cd"]


def test_add_source_updates_existing_by_name(tmp_path):
    p = tmp_path / "c.yaml"
    add_source("https://old", "ab", p)
    add_source("https://new", "ab", p)
    cfg = read_config(p)
    assert len(cfg["sources"]) == 1
    assert cfg["sources"][0]["url"] == "https://new"


def test_detect_targets_returns_list():
    # Just shape — don't depend on the developer's machine.
    result = detect_targets()
    assert isinstance(result, list)
    for t in result:
        assert "name" in t and "path" in t and "found" in t
