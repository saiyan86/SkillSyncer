"""Tests for skillsyncer.guarder."""

from __future__ import annotations

import yaml

from skillsyncer.guarder import guard_fix
from skillsyncer.scanner import scan_file


def _write_skill(skill_dir, body, manifest=None):
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(body, encoding="utf-8")
    if manifest is not None:
        (skill_dir / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False),
            encoding="utf-8",
        )
    return md


def test_replaces_known_identity_value(tmp_path):
    md = _write_skill(
        tmp_path / "energy",
        "Use sk-abcdefghij1234567890 for auth\n",
        manifest={"name": "energy"},
    )
    identity = {"secrets": {"GATEWAY_KEY": "sk-abcdefghij1234567890"}}
    detections = scan_file(md, identity["secrets"])
    fixes = guard_fix(tmp_path, identity, detections)

    assert md.read_text() == "Use ${{GATEWAY_KEY}} for auth\n"
    assert len(fixes) == 1
    assert fixes[0]["status"] == "fixed"
    assert fixes[0]["replacement"] == "${{GATEWAY_KEY}}"
    assert fixes[0]["identity_key"] == "GATEWAY_KEY"


def test_unresolved_when_no_identity_key(tmp_path):
    md = _write_skill(
        tmp_path / "raw",
        "leak: sk-abcdefghij1234567890\n",
    )
    detections = scan_file(md, {})  # no identity → no key on detection
    fixes = guard_fix(tmp_path, {}, detections)
    assert len(fixes) == 1
    assert fixes[0]["status"] == "unresolved"
    assert fixes[0]["replacement"] is None
    # File must be unchanged.
    assert "sk-abcdefghij1234567890" in md.read_text()


def test_manifest_updated_with_new_placeholder(tmp_path):
    md = _write_skill(
        tmp_path / "energy",
        "url: https://gw.site-a.example.com/api\n",
        manifest={"name": "energy", "requires": {"secrets": []}},
    )
    identity = {"secrets": {"GATEWAY_URL": "https://gw.site-a.example.com"}}
    detections = scan_file(md, identity["secrets"])
    guard_fix(tmp_path, identity, detections)

    manifest = yaml.safe_load((tmp_path / "energy" / "manifest.yaml").read_text())
    names = [s["name"] if isinstance(s, dict) else s for s in manifest["requires"]["secrets"]]
    assert "GATEWAY_URL" in names


def test_manifest_not_duplicated(tmp_path):
    md = _write_skill(
        tmp_path / "energy",
        "url: https://gw.site-a.example.com/api\n",
        manifest={
            "name": "energy",
            "requires": {"secrets": [{"name": "GATEWAY_URL", "description": "x"}]},
        },
    )
    identity = {"secrets": {"GATEWAY_URL": "https://gw.site-a.example.com"}}
    detections = scan_file(md, identity["secrets"])
    guard_fix(tmp_path, identity, detections)

    manifest = yaml.safe_load((tmp_path / "energy" / "manifest.yaml").read_text())
    names = [s["name"] if isinstance(s, dict) else s for s in manifest["requires"]["secrets"]]
    assert names.count("GATEWAY_URL") == 1


def test_placeholder_already_present_no_change(tmp_path):
    md = _write_skill(tmp_path / "energy", "Use ${{GATEWAY_KEY}} here\n")
    identity = {"secrets": {"GATEWAY_KEY": "sk-abcdefghij1234567890"}}
    detections = scan_file(md, identity["secrets"])
    fixes = guard_fix(tmp_path, identity, detections)
    assert detections == []
    assert fixes == []
    assert md.read_text() == "Use ${{GATEWAY_KEY}} here\n"


def test_replaces_all_occurrences_in_file(tmp_path):
    md = _write_skill(
        tmp_path / "energy",
        "k=sk-abcdefghij1234567890 and again sk-abcdefghij1234567890\n",
    )
    identity = {"secrets": {"K": "sk-abcdefghij1234567890"}}
    detections = scan_file(md, identity["secrets"])
    guard_fix(tmp_path, identity, detections)
    assert "sk-abcdefghij1234567890" not in md.read_text()
    assert md.read_text().count("${{K}}") == 2


def test_no_manifest_file_does_not_error(tmp_path):
    md = _write_skill(tmp_path / "energy", "k=sk-abcdefghij1234567890\n")
    identity = {"secrets": {"K": "sk-abcdefghij1234567890"}}
    detections = scan_file(md, identity["secrets"])
    fixes = guard_fix(tmp_path, identity, detections)
    assert fixes[0]["status"] == "fixed"
    assert "${{K}}" in md.read_text()
