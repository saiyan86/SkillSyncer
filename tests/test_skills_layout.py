"""Tests for the repo's thin-harness / fat-skills layout.

These lock in the invariants introduced when operator/SKILL.md was
split from a single ~200-line document into a thin resolver plus
eight standalone skills under skills/.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
OPERATOR = REPO_ROOT / "operator" / "SKILL.md"

EXPECTED_SKILLS = {
    "skillsyncer-onboard",
    "skillsyncer-fill",
    "skillsyncer-guard-assist",
    "skillsyncer-share",
    "skillsyncer-report",
    "skillsyncer-status",
    "skillsyncer-investigate",
    "skillsyncer-improve",
}


def test_every_expected_skill_exists_on_disk():
    found = {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()}
    assert EXPECTED_SKILLS.issubset(found), f"missing: {EXPECTED_SKILLS - found}"


def test_every_skill_has_a_manifest_and_skill_md():
    for name in EXPECTED_SKILLS:
        skill_dir = SKILLS_DIR / name
        assert (skill_dir / "manifest.yaml").is_file(), f"{name}: manifest.yaml missing"
        assert (skill_dir / "SKILL.md").is_file(), f"{name}: SKILL.md missing"


def test_every_manifest_has_required_fields():
    for name in EXPECTED_SKILLS:
        data = yaml.safe_load(
            (SKILLS_DIR / name / "manifest.yaml").read_text(encoding="utf-8")
        )
        assert data.get("name") == name, f"{name}: manifest.name wrong"
        assert data.get("version"), f"{name}: manifest.version missing"
        assert data.get("description"), f"{name}: manifest.description missing"


def test_every_skill_md_carries_the_preamble():
    """Every SKILL.md in skills/ must include the bootstrap preamble
    so downstream users get auto-install when they pull this repo."""
    for name in EXPECTED_SKILLS:
        content = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert "skillsyncer:require" in content, (
            f"{name}: SKILL.md missing the skillsyncer:require preamble"
        )


def test_every_skill_md_has_a_title_and_when_to_activate():
    """Fat skills follow a convention: H1 title, a 'When to activate'
    section, and a 'Process' or numbered procedure."""
    for name in EXPECTED_SKILLS:
        content = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert re.search(r"(?m)^# ", content), f"{name}: missing H1 title"
        assert "When to activate" in content, f"{name}: missing 'When to activate' section"


def test_parameterized_skills_list_parameters():
    """Skills that take parameters must list them in a Parameters
    table so the resolver knows how to invoke them."""
    parameterized = {"skillsyncer-investigate", "skillsyncer-improve",
                     "skillsyncer-share", "skillsyncer-fill",
                     "skillsyncer-guard-assist", "skillsyncer-report"}
    for name in parameterized:
        content = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert "## Parameters" in content or "Parameters" in content, (
            f"{name}: parameterized skill missing a 'Parameters' section"
        )


def test_resolver_points_at_every_skill():
    """operator/SKILL.md is the resolver; it must name every skill
    in the skills/ tree, otherwise the split is incomplete."""
    text = OPERATOR.read_text(encoding="utf-8")
    for name in EXPECTED_SKILLS:
        assert name in text, f"operator/SKILL.md does not reference {name}"


def test_resolver_is_actually_thin():
    """The resolver should be short and dense — not a re-mash of
    every procedure. Lock in a line-count upper bound so it can't
    drift back into a fat file by accident."""
    lines = OPERATOR.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 120, (
        f"operator/SKILL.md is {len(lines)} lines; should be < 120 "
        f"(a thin resolver, not a re-combined fat skill)"
    )


def test_skills_index_exists_and_lists_every_skill():
    index = SKILLS_DIR / "INDEX.md"
    assert index.is_file(), "skills/INDEX.md missing"
    text = index.read_text(encoding="utf-8")
    for name in EXPECTED_SKILLS:
        assert name in text, f"skills/INDEX.md does not list {name}"
