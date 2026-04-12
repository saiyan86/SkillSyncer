"""End-to-end tests for the click CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from skillsyncer.cli import main


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated SkillSyncer home for each test."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("SKILLSYNCER_HOME", str(h))
    return h


@pytest.fixture
def runner():
    return CliRunner()


def _make_skill(repo, name, body, manifest=None):
    skill = repo / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(body)
    if manifest is not None:
        (skill / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return skill


def test_init_creates_home(home, runner):
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    assert (home / "config.yaml").exists()
    assert (home / "identity.yaml").exists()


def test_init_json_excludes_credential_values(home, runner, tmp_path, monkeypatch):
    # Make sure values never leak into init output, even when env has secrets.
    monkeypatch.setenv("API_KEY", "sk-very-secret-value-1234567890")
    result = runner.invoke(main, ["init", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "credentials" in payload
    for c in payload["credentials"]:
        assert "value" not in c
    assert "sk-very-secret-value-1234567890" not in result.output


def test_secret_set_and_list(home, runner):
    runner.invoke(main, ["init"])
    result = runner.invoke(main, ["secret-set", "API_KEY", "sk-xyz"])
    assert result.exit_code == 0
    listing = runner.invoke(main, ["secret-list"])
    assert "API_KEY" in listing.output
    # Value must NEVER appear.
    assert "sk-xyz" not in listing.output


def test_render_pipeline(home, runner, tmp_path):
    runner.invoke(main, ["init"])
    src = tmp_path / "src"
    _make_skill(src, "energy", "URL=${{GATEWAY_URL}}", {"name": "energy"})
    target = tmp_path / "target"
    target.mkdir()

    config = {
        "sources": [{"name": "main", "path": str(src)}],
        "targets": [{"name": "claude-code", "path": str(target)}],
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))

    runner.invoke(main, ["secret-set", "GATEWAY_URL", "https://x"])
    result = runner.invoke(main, ["render"])
    assert result.exit_code == 0, result.output
    rendered = (target / "energy" / "SKILL.md").read_text()
    assert rendered == "URL=https://x"


def test_render_exit_1_when_unfilled(home, runner, tmp_path):
    runner.invoke(main, ["init"])
    src = tmp_path / "src"
    _make_skill(src, "alerting", "Need ${{MISSING_KEY}}")
    target = tmp_path / "target"
    target.mkdir()
    config = {
        "sources": [{"name": "s", "path": str(src)}],
        "targets": [{"name": "t", "path": str(target)}],
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))

    result = runner.invoke(main, ["render"])
    assert result.exit_code == 1
    assert "MISSING_KEY" in result.output


def test_fill_auto_from_env(home, runner, tmp_path, monkeypatch):
    runner.invoke(main, ["init"])
    src = tmp_path / "src"
    _make_skill(
        src, "alerting", "k=${{ALERT_KEY}}",
        {"name": "alerting", "requires": {"secrets": ["ALERT_KEY"]}},
    )
    config = {
        "sources": [{"name": "s", "path": str(src)}],
        "targets": [],
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))

    monkeypatch.setenv("ALERT_KEY", "from-env-value")
    result = runner.invoke(main, ["fill", "--auto"])
    assert result.exit_code == 0, result.output

    identity = yaml.safe_load((home / "identity.yaml").read_text())
    assert identity["secrets"]["ALERT_KEY"] == "from-env-value"


def test_fill_auto_exit_1_when_nothing_new(home, runner, tmp_path):
    runner.invoke(main, ["init"])
    config = {"sources": [], "targets": []}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    result = runner.invoke(main, ["fill", "--auto"])
    assert result.exit_code == 1


def test_scan_clean(home, runner, tmp_path):
    runner.invoke(main, ["init"])
    f = tmp_path / "ok.md"
    f.write_text("just text\n")
    result = runner.invoke(main, ["scan", "--path", str(f)])
    assert result.exit_code == 0


def test_scan_finds_secret_json(home, runner, tmp_path):
    runner.invoke(main, ["init"])
    f = tmp_path / "leak.md"
    f.write_text("token sk-abcdefghij1234567890\n")
    result = runner.invoke(main, ["scan", "--path", str(f), "--format", "json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert len(payload["detections"]) == 1


def test_guard_fix_replaces_known_secret(home, runner, tmp_path):
    runner.invoke(main, ["init"])
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    leaky = repo / "SKILL.md"
    leaky.write_text("k=sk-abcdefghij1234567890\n")
    subprocess.run(["git", "add", "SKILL.md"], cwd=repo, check=True)

    runner.invoke(main, ["secret-set", "API_KEY", "sk-abcdefghij1234567890"])
    result = runner.invoke(main, ["guard", "--fix", "--path", str(repo)])
    # All detections were resolvable → exit 0.
    assert result.exit_code == 0, result.output
    assert leaky.read_text() == "k=${{API_KEY}}\n"


def test_diff_since_last_sync_outputs_changed(home, runner, tmp_path):
    runner.invoke(main, ["init"])
    src = tmp_path / "src"
    _make_skill(src, "alpha", "v1")
    config = {"sources": [{"name": "s", "path": str(src)}], "targets": []}
    (home / "config.yaml").write_text(yaml.safe_dump(config))

    result = runner.invoke(main, ["diff-since-last-sync"])
    assert result.exit_code == 0
    assert "alpha" in result.output


def test_status_smoke(home, runner, tmp_path):
    runner.invoke(main, ["init"])
    src = tmp_path / "src"
    _make_skill(
        src, "energy", "k=${{K}}",
        {"name": "energy", "requires": {"secrets": [{"name": "K", "description": "the key"}]}},
    )
    config = {"sources": [{"name": "s", "path": str(src)}], "targets": []}
    (home / "config.yaml").write_text(yaml.safe_dump(config))

    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "energy" in result.output
    assert "K" in result.output


def test_report_lifecycle(home, runner):
    runner.invoke(main, ["init"])
    create = runner.invoke(main, ["report", "create", "--type", "guard"])
    assert create.exit_code == 0
    report_path = create.output.strip()
    assert Path(report_path).exists()

    update = runner.invoke(main, ["report", "update", report_path, "--attempt", "1", "--issues", "[]"])
    assert update.exit_code == 0

    finalize = runner.invoke(main, ["report", "finalize", report_path, "--status", "passed"])
    assert finalize.exit_code == 0

    status = runner.invoke(main, ["report", "status", report_path])
    assert status.exit_code == 0
    assert "passed" in status.output

    latest = runner.invoke(main, ["report", "latest", "--type", "guard"])
    assert latest.exit_code == 0
    assert "passed" in latest.output
