"""Tests for skillsyncer.state."""

from __future__ import annotations

from skillsyncer.state import (
    get_drift,
    hash_file,
    read_state,
    update_skill_state,
    write_state,
)


def test_read_missing_returns_empty(tmp_path):
    assert read_state(tmp_path / "s.yaml") == {"skills": {}}


def test_write_then_read(tmp_path):
    p = tmp_path / "s.yaml"
    write_state({"skills": {"a": {"hash": "abc", "version": "0.1"}}}, p)
    state = read_state(p)
    assert state["skills"]["a"]["hash"] == "abc"


def test_update_skill_state_merges(tmp_path):
    p = tmp_path / "s.yaml"
    update_skill_state("alpha", path=p, hash="h1", version="0.1")
    update_skill_state("alpha", path=p, last_rendered=123)
    state = read_state(p)
    assert state["skills"]["alpha"]["hash"] == "h1"
    assert state["skills"]["alpha"]["last_rendered"] == 123


def test_get_drift_detects_changes(tmp_path):
    source = tmp_path / "src"
    skill = source / "alpha"
    skill.mkdir(parents=True)
    md = skill / "SKILL.md"
    md.write_text("v1")

    state_path = tmp_path / "state.yaml"
    config = {"sources": [{"name": "s", "path": str(source)}]}

    drift = get_drift(config, state_path)
    assert len(drift) == 1
    assert drift[0]["name"] == "alpha"
    assert drift[0]["recorded_hash"] is None

    update_skill_state("alpha", path=state_path, hash=hash_file(md))
    assert get_drift(config, state_path) == []

    md.write_text("v2")
    assert len(get_drift(config, state_path)) == 1
