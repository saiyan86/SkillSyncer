"""Tests for skillsyncer.renderer."""

from __future__ import annotations

from pathlib import Path

from skillsyncer.renderer import render_skill, render_all_skills


def test_basic_substitution():
    out, unfilled = render_skill(
        "Connect to ${{GATEWAY_URL}}",
        manifest={"name": "x"},
        identity={"secrets": {"GATEWAY_URL": "https://x"}},
    )
    assert out == "Connect to https://x"
    assert unfilled == []


def test_unfilled_left_as_is():
    out, unfilled = render_skill("${{MISSING}}", manifest={}, identity={})
    assert out == "${{MISSING}}"
    assert unfilled == ["MISSING"]


def test_unfilled_dedup_preserves_order():
    out, unfilled = render_skill(
        "${{A}} ${{B}} ${{A}} ${{C}}",
        manifest={},
        identity={},
    )
    assert unfilled == ["A", "B", "C"]
    assert out == "${{A}} ${{B}} ${{A}} ${{C}}"


def test_resolution_order_overrides_beats_secrets_beats_values():
    manifest = {"name": "energy", "values": {"K": "from-values"}}
    identity = {
        "secrets": {"K": "from-secrets"},
        "overrides": {"energy": {"K": "from-overrides"}},
    }
    out, unfilled = render_skill("${{K}}", manifest, identity)
    assert out == "from-overrides"
    assert unfilled == []

    # Drop overrides → secrets win
    identity2 = {"secrets": {"K": "from-secrets"}}
    out2, _ = render_skill("${{K}}", manifest, identity2)
    assert out2 == "from-secrets"

    # Drop secrets too → manifest values win
    out3, _ = render_skill("${{K}}", manifest, {})
    assert out3 == "from-values"


def test_overrides_only_apply_to_matching_skill_name():
    manifest = {"name": "alpha", "values": {}}
    identity = {
        "secrets": {"K": "secret"},
        "overrides": {"beta": {"K": "wrong"}},
    }
    out, _ = render_skill("${{K}}", manifest, identity)
    assert out == "secret"


def test_empty_identity():
    out, unfilled = render_skill(
        "${{X}} and ${{Y}}",
        manifest={"values": {"X": "1"}},
        identity=None,
    )
    assert out == "1 and ${{Y}}"
    assert unfilled == ["Y"]


def test_lowercase_keys_not_substituted():
    # PLACEHOLDER_RE only matches uppercase identifiers.
    out, unfilled = render_skill("${{lower}}", {}, {})
    assert out == "${{lower}}"
    assert unfilled == []


def test_render_all_skills_writes_to_targets(tmp_path):
    source = tmp_path / "src"
    skill = source / "energy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("URL=${{GATEWAY_URL}}")
    (skill / "manifest.yaml").write_text("name: energy\n")

    target = tmp_path / "claude-skills"
    target.mkdir()

    config = {
        "sources": [{"name": "main", "path": str(source)}],
        "targets": [{"name": "claude-code", "path": str(target)}],
    }
    identity = {"secrets": {"GATEWAY_URL": "https://x"}}

    report = render_all_skills(config, identity)

    out_file = target / "energy" / "SKILL.md"
    assert out_file.read_text() == "URL=https://x"
    assert any(s["name"] == "energy" and s["unfilled"] == [] for s in report["skills"])
    assert report["unfilled"] == {}
    assert str(out_file) in report["written"]


def test_render_all_skills_reports_unfilled(tmp_path):
    source = tmp_path / "src"
    skill = source / "alerting"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Need ${{MISSING_KEY}}")
    target = tmp_path / "out"
    target.mkdir()

    config = {
        "sources": [{"name": "s", "path": str(source)}],
        "targets": [{"name": "t", "path": str(target)}],
    }
    report = render_all_skills(config, {"secrets": {}})
    assert report["unfilled"] == {"alerting": ["MISSING_KEY"]}
    out_file = target / "alerting" / "SKILL.md"
    assert "${{MISSING_KEY}}" in out_file.read_text()
