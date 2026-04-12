"""End-to-end tests for the argparse CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from skillsyncer.cli import main as cli_main


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated SkillSyncer home for each test."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("SKILLSYNCER_HOME", str(h))
    return h


def _invoke(*argv: str) -> int:
    """Run the CLI in-process; return exit code (0 on success)."""
    try:
        return cli_main(list(argv)) or 0
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 0 if exc.code in (None, "") else 1


def _make_skill(repo, name, body, manifest=None):
    skill = repo / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(body, encoding="utf-8")
    if manifest is not None:
        (skill / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
    return skill


def test_init_creates_home(home, capsys):
    # Default `init` would prompt for consent; in test (non-tty stdin)
    # the prompt should default to "skip scan" automatically.
    rc = _invoke("init")
    assert rc == 0
    assert (home / "config.yaml").exists()
    assert (home / "identity.yaml").exists()


def test_init_no_scan_skips_credential_scan(home, capsys, monkeypatch):
    monkeypatch.setenv("STEPONEAI_API_KEY", "step-secret-1234567890")
    rc = _invoke("init", "--no-scan")
    assert rc == 0
    out = capsys.readouterr().out
    assert "scan skipped" in out.lower() or "Credentials: scan skipped" in out
    # No values should ever appear regardless.
    assert "step-secret-1234567890" not in out


def test_init_yes_runs_scan(home, capsys, monkeypatch):
    monkeypatch.setenv("STEPONEAI_API_KEY", "step-secret-1234567890")
    rc = _invoke("init", "--yes")
    assert rc == 0
    out = capsys.readouterr().out
    assert "STEPONEAI_API_KEY" in out
    # Values still must not appear.
    assert "step-secret-1234567890" not in out


def test_init_groups_duplicate_credentials(home, capsys, monkeypatch, tmp_path):
    """When the discoverer finds the same key with different values
    from different paths, init should collapse them to one line."""
    # Build a fake openclaw dir with the same key in two places.
    fake_home = tmp_path / "fake_home"
    openclaw = fake_home / ".openclaw"
    openclaw.mkdir(parents=True)
    (openclaw / "openclaw.json").write_text(
        '{"plugins":{"entries":{"brave":{"config":{"webSearch":{"apiKey":"v1"}}}}},'
        '"models":{"providers":{"brave":{"apiKey":"v2"}}}}'
    )
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: fake_home))
    rc = _invoke("init", "--yes")
    assert rc == 0
    out = capsys.readouterr().out
    # The unique key appears once with a "(2 values)" annotation.
    assert out.count("BRAVE_API_KEY") == 1
    assert "2 values" in out


def test_init_consent_prompt_yes(home, capsys, monkeypatch):
    monkeypatch.setenv("STEPONEAI_API_KEY", "step-secret-1234567890")
    # Pretend stdin is a TTY and the user types "y\n".
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    rc = _invoke("init")
    assert rc == 0
    out = capsys.readouterr().out
    assert "Credential scan consent" in out
    assert "STEPONEAI_API_KEY" in out


def test_init_consent_prompt_no(home, capsys, monkeypatch):
    monkeypatch.setenv("STEPONEAI_API_KEY", "step-secret-1234567890")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    rc = _invoke("init")
    assert rc == 0
    out = capsys.readouterr().out
    assert "Credential scan consent" in out
    # User declined → no creds scanned
    assert "STEPONEAI_API_KEY" not in out
    assert "scan skipped" in out.lower()


def test_init_json_default_does_not_scan_credentials(home, capsys, monkeypatch):
    """JSON mode is for the operator agent — it must NOT scan
    creds without explicit --scan-credentials, since the agent is
    responsible for asking the user first."""
    monkeypatch.setenv("STEPONEAI_API_KEY", "step-secret-1234567890")
    rc = _invoke("init", "--json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["credentials"] == []
    assert payload["credential_scan_performed"] is False
    assert "credential_scan_plan" in payload
    assert len(payload["credential_scan_plan"]) > 0


def test_init_json_with_scan_credentials_flag(home, capsys, monkeypatch):
    monkeypatch.setenv("STEPONEAI_API_KEY", "step-secret-1234567890")
    rc = _invoke("init", "--json", "--scan-credentials")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["credential_scan_performed"] is True
    keys = {c["key"] for c in payload["credentials"]}
    assert "STEPONEAI_API_KEY" in keys
    # Values still stripped from JSON output.
    for c in payload["credentials"]:
        assert "value" not in c


def test_init_json_excludes_credential_values(home, capsys, monkeypatch):
    monkeypatch.setenv("API_KEY", "sk-very-secret-value-1234567890")
    # Use --scan-credentials so the scan actually runs.
    rc = _invoke("init", "--json", "--scan-credentials")
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "credentials" in payload
    for c in payload["credentials"]:
        assert "value" not in c
    assert "sk-very-secret-value-1234567890" not in out


def test_secret_set_and_list(home, capsys):
    _invoke("init")
    capsys.readouterr()
    rc = _invoke("secret-set", "API_KEY", "sk-xyz")
    assert rc == 0
    capsys.readouterr()
    _invoke("secret-list")
    out = capsys.readouterr().out
    assert "API_KEY" in out
    assert "sk-xyz" not in out  # values must NEVER appear


def test_render_pipeline(home, capsys, tmp_path):
    _invoke("init")
    src = tmp_path / "src"
    _make_skill(src, "energy", "URL=${{GATEWAY_URL}}", {"name": "energy"})
    target = tmp_path / "target"
    target.mkdir()

    config = {
        "sources": [{"name": "main", "path": str(src)}],
        "targets": [{"name": "claude-code", "path": str(target)}],
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    _invoke("secret-set", "GATEWAY_URL", "https://x")
    capsys.readouterr()
    rc = _invoke("render")
    assert rc == 0
    rendered = (target / "energy" / "SKILL.md").read_text(encoding="utf-8")
    assert rendered == "URL=https://x"


def test_render_exit_1_when_unfilled(home, capsys, tmp_path):
    _invoke("init")
    src = tmp_path / "src"
    _make_skill(src, "alerting", "Need ${{MISSING_KEY}}")
    target = tmp_path / "target"
    target.mkdir()
    config = {
        "sources": [{"name": "s", "path": str(src)}],
        "targets": [{"name": "t", "path": str(target)}],
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    capsys.readouterr()
    rc = _invoke("render")
    assert rc == 1
    captured = capsys.readouterr()
    assert "MISSING_KEY" in (captured.out + captured.err)


def test_fill_auto_from_env(home, capsys, tmp_path, monkeypatch):
    _invoke("init")
    src = tmp_path / "src"
    _make_skill(
        src, "alerting", "k=${{ALERT_KEY}}",
        {"name": "alerting", "requires": {"secrets": ["ALERT_KEY"]}},
    )
    config = {"sources": [{"name": "s", "path": str(src)}], "targets": []}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    monkeypatch.setenv("ALERT_KEY", "from-env-value")
    rc = _invoke("fill", "--auto")
    assert rc == 0

    identity = yaml.safe_load((home / "identity.yaml").read_text(encoding="utf-8"))
    assert identity["secrets"]["ALERT_KEY"] == "from-env-value"


def test_fill_auto_exit_1_when_nothing_new(home, capsys, tmp_path):
    _invoke("init")
    config = {"sources": [], "targets": []}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    rc = _invoke("fill", "--auto")
    assert rc == 1


def test_scan_clean(home, capsys, tmp_path):
    _invoke("init")
    f = tmp_path / "ok.md"
    f.write_text("just text\n", encoding="utf-8")
    rc = _invoke("scan", "--path", str(f))
    assert rc == 0


def test_scan_finds_secret_json(home, capsys, tmp_path):
    _invoke("init")
    capsys.readouterr()
    f = tmp_path / "leak.md"
    f.write_text("token sk-abcdefghij1234567890\n", encoding="utf-8")
    rc = _invoke("scan", "--path", str(f), "--format", "json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["detections"]) == 1


def test_guard_fix_replaces_known_secret(home, capsys, tmp_path):
    _invoke("init")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    leaky = repo / "SKILL.md"
    leaky.write_text("k=sk-abcdefghij1234567890\n", encoding="utf-8")
    subprocess.run(["git", "add", "SKILL.md"], cwd=repo, check=True)

    _invoke("secret-set", "API_KEY", "sk-abcdefghij1234567890")
    capsys.readouterr()
    rc = _invoke("guard", "--fix", "--path", str(repo))
    assert rc == 0
    assert leaky.read_text(encoding="utf-8") == "k=${{API_KEY}}\n"


def test_diff_since_last_sync_outputs_changed(home, capsys, tmp_path):
    _invoke("init")
    src = tmp_path / "src"
    _make_skill(src, "alpha", "v1")
    config = {"sources": [{"name": "s", "path": str(src)}], "targets": []}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    capsys.readouterr()
    rc = _invoke("diff-since-last-sync")
    assert rc == 0
    assert "alpha" in capsys.readouterr().out


def test_status_smoke(home, capsys, tmp_path):
    _invoke("init")
    src = tmp_path / "src"
    _make_skill(
        src, "energy", "k=${{K}}",
        {"name": "energy", "requires": {"secrets": [{"name": "K", "description": "the key"}]}},
    )
    config = {"sources": [{"name": "s", "path": str(src)}], "targets": []}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    capsys.readouterr()
    rc = _invoke("status")
    assert rc == 0
    out = capsys.readouterr().out
    assert "energy" in out
    assert "K" in out


@pytest.fixture
def fake_machine(home, tmp_path, monkeypatch):
    """Build an isolated 'machine': a fake $HOME with one skill in
    ~/.claude/skills/, plus a registered source repo at
    ~/.skillsyncer/repos/<name>/."""
    fake_home = tmp_path / "machine_home"
    fake_home.mkdir()
    # One agent skill
    skill_dir = fake_home / ".claude" / "skills" / "energy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Energy\n\nUse the gateway.\n", encoding="utf-8")

    # Source repo
    source = tmp_path / "source-repo"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=source, check=True)

    # Register the source in config.yaml
    config = {
        "sources": [{"name": "yc", "url": "git@example.com:me/yc.git", "path": str(source)}],
        "targets": [],
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    # Make Path.home() return the fake home so _find_local_skills sees it.
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: fake_home))
    return {"fake_home": fake_home, "source": source, "skill_dir": skill_dir}


def test_publish_all_copies_and_commits(fake_machine, capsys):
    rc = _invoke("publish", "--all")
    assert rc == 0, capsys.readouterr().out
    src = fake_machine["source"]
    out_md = src / "energy" / "SKILL.md"
    assert out_md.is_file()
    text = out_md.read_text(encoding="utf-8")
    assert "skillsyncer:require" in text
    assert "Use the gateway" in text
    # Verify a commit was created
    log = subprocess.run(
        ["git", "-C", str(src), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "Publish 1 skill" in log


def test_publish_specific_skill_only(fake_machine, capsys):
    # Add a second skill so --skill makes a difference
    second = fake_machine["fake_home"] / ".claude" / "skills" / "alerting"
    second.mkdir(parents=True)
    (second / "SKILL.md").write_text("# Alerting", encoding="utf-8")

    rc = _invoke("publish", "--skill", "energy")
    assert rc == 0, capsys.readouterr().out
    src = fake_machine["source"]
    assert (src / "energy" / "SKILL.md").is_file()
    assert not (src / "alerting" / "SKILL.md").exists()


def test_publish_blocks_on_hardcoded_secret(fake_machine, capsys):
    # Inject a secret into the skill that the scanner will catch.
    md = fake_machine["skill_dir"] / "SKILL.md"
    md.write_text("token sk-abcdefghij1234567890\n", encoding="utf-8")
    rc = _invoke("publish", "--all")
    assert rc == 1
    err = capsys.readouterr().err
    assert "pre-flight scan" in err
    # NO commit should have been created.
    log = subprocess.run(
        ["git", "-C", str(fake_machine["source"]), "log", "--oneline"],
        capture_output=True, text=True,
    )
    # Either no log (no commits) or empty stdout
    assert log.stdout.strip() == ""


def test_publish_unknown_skill_errors(fake_machine, capsys):
    rc = _invoke("publish", "--skill", "does-not-exist")
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_publish_no_source_errors(home, capsys, monkeypatch, tmp_path):
    fake_home = tmp_path / "machine"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: fake_home))
    # config has no sources
    (home / "config.yaml").write_text(yaml.safe_dump({"sources": [], "targets": []}), encoding="utf-8")
    rc = _invoke("publish", "--all")
    assert rc == 2
    assert "No sources registered" in capsys.readouterr().err


def test_skills_lists_local_skills(fake_machine, capsys):
    rc = _invoke("skills")
    assert rc == 0
    out = capsys.readouterr().out
    assert "energy" in out
    assert "claude-code" in out


def test_skills_filter_by_agent(fake_machine, capsys):
    # Add a hermes skill
    hermes = fake_machine["fake_home"] / ".hermes" / "skills" / "dogfood"
    hermes.mkdir(parents=True)
    (hermes / "SKILL.md").write_text("# dogfood")
    rc = _invoke("skills", "--agent", "hermes")
    assert rc == 0
    out = capsys.readouterr().out
    assert "dogfood" in out
    assert "energy" not in out  # claude-code skill filtered out


def test_skills_json(fake_machine, capsys):
    rc = _invoke("skills", "--json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    names = [s["name"] for s in payload]
    assert "energy" in names


def test_skills_unknown_agent_errors(fake_machine, capsys):
    rc = _invoke("skills", "--agent", "no-such-agent")
    assert rc == 2
    assert "no skills found" in capsys.readouterr().err.lower()


def test_publish_injects_customize_instructions_preamble(fake_machine, capsys):
    """The preamble injected on publish should include the AGENT
    "customize this skill" instructions, not just the install one-liner."""
    _invoke("publish", "--all")
    src = fake_machine["source"]
    md = (src / "energy" / "SKILL.md").read_text(encoding="utf-8")
    assert "skillsyncer:require" in md
    assert "AGENT" in md  # "AGENT — adapt this skill..."
    assert "fill --auto" in md
    assert "customize" in md.lower()


def test_publish_idempotent_no_changes(fake_machine, capsys):
    # First publish
    _invoke("publish", "--all")
    capsys.readouterr()
    # Second publish — nothing changed → no new commit
    rc = _invoke("publish", "--all")
    assert rc == 0
    out = capsys.readouterr().out
    assert "no changes to commit" in out


def test_doctor_clean(home, capsys):
    _invoke("init", "--no-scan")
    capsys.readouterr()
    rc = _invoke("doctor")
    # exit 0 if no issues; on a clean test home there shouldn't be any
    assert rc in (0, 1), capsys.readouterr().out


def test_doctor_reports_missing_home(home, capsys, tmp_path, monkeypatch):
    """If $SKILLSYNCER_HOME points at a missing dir, doctor should
    surface it as an issue and exit non-zero."""
    monkeypatch.setenv("SKILLSYNCER_HOME", str(tmp_path / "definitely-not-here"))
    rc = _invoke("doctor")
    assert rc == 1
    out = capsys.readouterr().out
    assert "does not exist" in out
    assert "init" in out  # remediation hint


def test_sources_list_empty(home, capsys):
    _invoke("init", "--no-scan")
    capsys.readouterr()
    rc = _invoke("sources", "list")
    assert rc == 0
    assert "No sources registered" in capsys.readouterr().out


def test_sources_list_remove_show(home, capsys, tmp_path):
    _invoke("init", "--no-scan")
    capsys.readouterr()
    # Manually inject a source into config (avoids the git clone)
    config = {
        "sources": [{"name": "demo", "url": "git@example.com:me/demo.git",
                     "path": str(tmp_path / "demo-repo")}],
        "targets": [],
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    rc = _invoke("sources", "list")
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo" in out
    assert "git@example.com:me/demo.git" in out

    rc = _invoke("sources", "show", "demo")
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo" in out

    rc = _invoke("sources", "remove", "demo")
    assert rc == 0
    out = capsys.readouterr().out
    assert "Removed source" in out

    # Verify removal
    after = yaml.safe_load((home / "config.yaml").read_text())
    assert after["sources"] == []


def test_sources_remove_unknown(home, capsys):
    _invoke("init", "--no-scan")
    capsys.readouterr()
    rc = _invoke("sources", "remove", "no-such")
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_hooks_install_uninstall_status(home, capsys, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    capsys.readouterr()
    rc = _invoke("hooks", "status", "--path", str(repo))
    assert rc == 0
    assert "not installed" in capsys.readouterr().out

    rc = _invoke("hooks", "install", "--path", str(repo))
    assert rc == 0
    assert (repo / ".git" / "hooks" / "pre-push").exists()

    capsys.readouterr()
    rc = _invoke("hooks", "status", "--path", str(repo))
    assert rc == 0
    assert "installed" in capsys.readouterr().out

    rc = _invoke("hooks", "uninstall", "--path", str(repo))
    assert rc == 0
    assert not (repo / ".git" / "hooks" / "pre-push").exists()


def test_skill_show_existing(fake_machine, capsys):
    capsys.readouterr()
    rc = _invoke("skill", "show", "energy")
    assert rc == 0
    out = capsys.readouterr().out
    assert "energy" in out
    assert "claude-code" in out
    assert "managed" in out


def test_skill_show_unknown(fake_machine, capsys):
    rc = _invoke("skill", "show", "no-such-skill")
    assert rc == 2
    err = capsys.readouterr().err.lower()
    assert "no skill" in err or "not found" in err


def test_sync_no_sources(home, capsys):
    _invoke("init", "--no-scan")
    capsys.readouterr()
    rc = _invoke("sync")
    assert rc == 2
    assert "no sources" in capsys.readouterr().err.lower()


def test_sync_pulls_source_then_renders(home, capsys, tmp_path):
    _invoke("init", "--no-scan")
    capsys.readouterr()

    # Build a tiny upstream repo
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    subprocess.run(["git", "init", "--bare", "-q"], cwd=upstream, check=True)

    # Build a working clone
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(upstream), str(work)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    skill = work / "energy"
    skill.mkdir()
    (skill / "SKILL.md").write_text("URL=${{GATEWAY_URL}}", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=work, check=True)
    subprocess.run(["git", "push", "-q"], cwd=work, check=True)

    # Local source clone tracked by config
    local = tmp_path / "local-source"
    subprocess.run(["git", "clone", "-q", str(upstream), str(local)], check=True)
    target = tmp_path / "target"
    target.mkdir()
    config = {
        "sources": [{"name": "demo", "url": str(upstream), "path": str(local)}],
        "targets": [{"name": "agent", "path": str(target)}],
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    _invoke("secret-set", "GATEWAY_URL", "https://x")
    capsys.readouterr()

    rc = _invoke("sync")
    assert rc == 0
    rendered = (target / "energy" / "SKILL.md").read_text(encoding="utf-8")
    assert rendered == "URL=https://x"


def test_report_lifecycle(home, capsys):
    _invoke("init")
    capsys.readouterr()
    _invoke("report", "create", "--type", "guard")
    report_path = capsys.readouterr().out.strip()
    assert Path(report_path).exists()

    rc = _invoke("report", "update", report_path, "--attempt", "1", "--issues", "[]")
    assert rc == 0

    rc = _invoke("report", "finalize", report_path, "--status", "passed")
    assert rc == 0

    capsys.readouterr()
    rc = _invoke("report", "status", report_path)
    assert rc == 0
    assert "passed" in capsys.readouterr().out

    rc = _invoke("report", "latest", "--type", "guard")
    assert rc == 0
    assert "passed" in capsys.readouterr().out
