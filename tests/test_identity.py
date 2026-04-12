"""Tests for skillsyncer.identity."""

from __future__ import annotations

import yaml

from skillsyncer.identity import (
    list_secret_keys,
    read_identity,
    set_secret,
    write_identity,
)


def test_read_missing_returns_empty(tmp_path):
    p = tmp_path / "id.yaml"
    data = read_identity(p)
    assert data == {"secrets": {}, "overrides": {}}


def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "id.yaml"
    write_identity({"secrets": {"K": "v"}, "overrides": {"skill": {"x": 1}}}, p)
    data = read_identity(p)
    assert data["secrets"]["K"] == "v"
    assert data["overrides"]["skill"]["x"] == 1


def test_set_secret_creates_file(tmp_path):
    p = tmp_path / "nested" / "id.yaml"
    set_secret("API_KEY", "sk-xyz", p)
    assert p.exists()
    assert read_identity(p)["secrets"]["API_KEY"] == "sk-xyz"


def test_set_secret_updates_existing(tmp_path):
    p = tmp_path / "id.yaml"
    set_secret("K", "v1", p)
    set_secret("K", "v2", p)
    assert read_identity(p)["secrets"]["K"] == "v2"


def test_list_secret_keys_sorted(tmp_path):
    p = tmp_path / "id.yaml"
    write_identity({"secrets": {"B": "x", "A": "y", "C": "z"}}, p)
    assert list_secret_keys(p) == ["A", "B", "C"]


def test_read_handles_null_sections(tmp_path):
    p = tmp_path / "id.yaml"
    p.write_text("secrets:\noverrides:\n")
    data = read_identity(p)
    assert data == {"secrets": {}, "overrides": {}}


def test_write_uses_atomic_replace(tmp_path):
    p = tmp_path / "id.yaml"
    write_identity({"secrets": {"K": "v"}}, p)
    raw = yaml.safe_load(p.read_text())
    assert raw["secrets"] == {"K": "v"}
