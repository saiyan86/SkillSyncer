"""Tests for skillsyncer.renderer."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from skillsyncer.renderer import render_skill, render_all_skills, render_skill_dir

FIXTURES = Path(__file__).parent / "fixtures"


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


# ---------------------------------------------------------------------------
# Full-directory rendering: assets, subdirs, binary preservation
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_render_copies_assets_directory_with_binary_intact(tmp_path):
    """Regression for the edgenesis-pptx bug: render must copy
    sibling assets/, not just SKILL.md. The PNG must be byte-identical."""
    source_skill = FIXTURES / "sample_skill_with_assets"
    assert (source_skill / "assets" / "logo.png").is_file()

    src = tmp_path / "src"
    src.mkdir()
    shutil.copytree(source_skill, src / "sample-with-assets")

    target = tmp_path / "target"
    target.mkdir()

    config = {
        "sources": [{"name": "fix", "path": str(src)}],
        "targets": [{"name": "agent", "path": str(target)}],
    }
    identity = {"secrets": {"GATEWAY_URL": "https://x"}}
    report = render_all_skills(config, identity)

    rendered_dir = target / "sample-with-assets"
    rendered_md = rendered_dir / "SKILL.md"
    rendered_logo = rendered_dir / "assets" / "logo.png"

    assert rendered_md.is_file(), "SKILL.md should still be rendered"
    assert rendered_logo.is_file(), "assets/logo.png must also be copied"

    # SKILL.md hydrated.
    assert "https://x" in rendered_md.read_text()
    # Binary asset is byte-identical.
    original_logo = source_skill / "assets" / "logo.png"
    assert _sha256(rendered_logo) == _sha256(original_logo)
    # Report references both files.
    assert any(p.endswith("SKILL.md") for p in report["written"])
    assert any(p.endswith("logo.png") for p in report["written"])


def test_render_skips_vcs_and_cache_dirs(tmp_path):
    src = tmp_path / "src"
    skill = src / "with-junk"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Hi")
    # Junk that should never be copied
    (skill / ".git").mkdir()
    (skill / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (skill / "node_modules").mkdir()
    (skill / "node_modules" / "leftpad.js").write_text("module.exports = ''")
    (skill / "__pycache__").mkdir()
    (skill / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
    (skill / ".DS_Store").write_bytes(b"\x00\x01")

    target = tmp_path / "out"
    target.mkdir()

    config = {
        "sources": [{"name": "s", "path": str(src)}],
        "targets": [{"name": "t", "path": str(target)}],
    }
    render_all_skills(config, {})

    out_skill = target / "with-junk"
    assert (out_skill / "SKILL.md").is_file()
    assert not (out_skill / ".git").exists()
    assert not (out_skill / "node_modules").exists()
    assert not (out_skill / "__pycache__").exists()
    assert not (out_skill / ".DS_Store").exists()


def test_render_preserves_nested_subdirectories(tmp_path):
    src = tmp_path / "src"
    skill = src / "deep"
    (skill / "templates" / "inner").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "references").mkdir()
    (skill / "SKILL.md").write_text("ok")
    (skill / "templates" / "inner" / "snippet.md").write_text("Use ${{X}}")
    (skill / "scripts" / "run.sh").write_text("#!/bin/sh\necho ${{X}}\n")
    (skill / "references" / "ref.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    target = tmp_path / "out"
    target.mkdir()
    config = {
        "sources": [{"name": "s", "path": str(src)}],
        "targets": [{"name": "t", "path": str(target)}],
    }
    render_all_skills(config, {"secrets": {"X": "yes"}})

    out_skill = target / "deep"
    assert (out_skill / "templates" / "inner" / "snippet.md").read_text() == "Use yes"
    assert (out_skill / "scripts" / "run.sh").read_text() == "#!/bin/sh\necho yes\n"
    assert (out_skill / "references" / "ref.png").read_bytes() == b"\x89PNG\r\n\x1a\n"


def test_render_skill_dir_unfilled_dedup_across_files(tmp_path):
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("${{A}} and ${{B}}")
    (skill / "config.yaml").write_text("a: ${{A}}\nc: ${{C}}\n")
    target = tmp_path / "out"
    target.mkdir()

    written, unfilled = render_skill_dir(
        skill, target, manifest={}, identity={"secrets": {}},
    )
    # First-appearance order across files: SKILL.md sorted before config.yaml.
    assert unfilled == ["A", "B", "C"]
    assert any(p.endswith("SKILL.md") for p in written)
    assert any(p.endswith("config.yaml") for p in written)
