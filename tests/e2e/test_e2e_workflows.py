"""End-to-end tests that exercise the real ``skillsyncer`` binary.

Every test here invokes the CLI via subprocess (full process boundary —
argparse, entry-point wiring, exit codes, stdout/stderr, on-disk side
effects), drives a multi-step user workflow, and asserts on real file
system state. They are slower than the in-process tests in
``tests/test_cli.py`` but catch packaging and integration regressions
the in-process tests can't.

Test isolation: each test gets its own ``$SKILLSYNCER_HOME`` and ``$HOME``
in a temp dir, so nothing escapes into the real user environment.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml

from .conftest import git, init_git_repo, write_config, write_skill

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# 1. Sanity — the binary is installed and self-describes.
# ---------------------------------------------------------------------------


def test_version_flag(run):
    result = run("--version")
    assert result.returncode == 0
    assert "skillsyncer" in result.stdout.lower()
    # Version follows semver-ish; just sanity-check there's a digit.
    assert any(ch.isdigit() for ch in result.stdout)


def test_help_lists_main_commands(run):
    result = run("--help")
    assert result.returncode == 0
    out = result.stdout
    for cmd in ("init", "add", "render", "sync", "publish", "scan", "guard",
                "hooks", "skills", "doctor", "status"):
        assert cmd in out, f"{cmd!r} missing from --help output"


def test_unknown_command_exits_nonzero(run):
    result = run("totally-not-a-command")
    assert result.returncode != 0
    # argparse writes usage / error to stderr.
    assert "invalid choice" in result.stderr or "unrecognized" in result.stderr


# ---------------------------------------------------------------------------
# 2. Initial setup — init creates the home tree.
# ---------------------------------------------------------------------------


def test_init_no_scan_creates_home_tree(run, home):
    result = run("init", "--no-scan", check=True)
    assert (home / "config.yaml").is_file()
    assert (home / "identity.yaml").is_file()
    assert "scan skipped" in result.output.lower()
    # Files must be valid YAML.
    yaml.safe_load((home / "config.yaml").read_text())
    yaml.safe_load((home / "identity.yaml").read_text())


def test_init_json_does_not_scan_credentials_by_default(run, monkeypatch):
    # The agent layer is responsible for asking for consent first;
    # `init --json` returns only the *plan* unless --scan-credentials
    # is also set. This is a load-bearing security guarantee.
    result = run(
        "init", "--json",
        env={"STEPONEAI_API_KEY": "step-secret-do-not-leak-1234567890"},
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["credential_scan_performed"] is False
    assert payload["credentials"] == []
    assert "credential_scan_plan" in payload
    # Defense-in-depth: secret value never appears in output.
    assert "step-secret-do-not-leak-1234567890" not in result.output


# ---------------------------------------------------------------------------
# 3. The render pipeline — the core "fill placeholders" workflow.
# ---------------------------------------------------------------------------


def test_render_pipeline_hydrates_placeholders(run, home, tmp_path):
    """init → register a local source → secret-set → render →
    rendered file in the agent target dir has the placeholder filled."""
    run("init", "--no-scan", check=True)

    src = tmp_path / "src"
    write_skill(src, "energy", "URL=${{GATEWAY_URL}}", manifest={"name": "energy"})
    target = tmp_path / "claude-skills"
    target.mkdir()

    write_config(home, {
        "sources": [{"name": "main", "path": str(src)}],
        "targets": [{"name": "claude-code", "path": str(target)}],
    })

    run("secret-set", "GATEWAY_URL", "https://api.example.com", check=True)

    result = run("render", check=True)
    rendered = (target / "energy" / "SKILL.md").read_text(encoding="utf-8")
    assert rendered == "URL=https://api.example.com"
    assert result.returncode == 0


def test_render_exits_1_when_placeholder_unfilled(run, home, tmp_path):
    """Render must surface unfilled placeholders by exiting 1 — git hooks
    rely on this exit code to gate pushes."""
    run("init", "--no-scan", check=True)

    src = tmp_path / "src"
    write_skill(src, "alerts", "Need ${{NO_SUCH_KEY}}")
    target = tmp_path / "agent"
    target.mkdir()
    write_config(home, {
        "sources": [{"name": "s", "path": str(src)}],
        "targets": [{"name": "t", "path": str(target)}],
    })

    result = run("render")
    assert result.returncode == 1
    assert "NO_SUCH_KEY" in result.output


# ---------------------------------------------------------------------------
# 4. Sync — clone-pull-render against a real (local) git remote.
# ---------------------------------------------------------------------------


def test_sync_pulls_upstream_then_renders(run, home, tmp_path):
    """End-to-end: bare upstream → working clone with skills → push →
    register the bare repo as a source → modify upstream → sync →
    rendered output reflects the upstream changes."""
    run("init", "--no-scan", check=True)

    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    git("init", "--bare", "-q", "-b", "main", cwd=upstream)

    work = tmp_path / "work"
    git("clone", "-q", str(upstream), str(work), cwd=tmp_path)
    git("config", "user.email", "test@example.com", cwd=work)
    git("config", "user.name", "Test", cwd=work)
    write_skill(work, "energy", "URL=${{GATEWAY_URL}}")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "initial skill", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)

    local = tmp_path / "local-source"
    git("clone", "-q", str(upstream), str(local), cwd=tmp_path)
    target = tmp_path / "agent-skills"
    target.mkdir()
    write_config(home, {
        "sources": [{"name": "demo", "url": str(upstream), "path": str(local)}],
        "targets": [{"name": "agent", "path": str(target)}],
    })
    run("secret-set", "GATEWAY_URL", "https://v1.example.com", check=True)

    # First sync — should render v1.
    run("sync", check=True)
    assert (target / "energy" / "SKILL.md").read_text() == "URL=https://v1.example.com"

    # Update the upstream and verify a second sync picks up the change.
    skill_md = work / "energy" / "SKILL.md"
    skill_md.write_text("URL=${{GATEWAY_URL}}/v2\n", encoding="utf-8")
    git("commit", "-aq", "-m", "v2", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)

    run("sync", check=True)
    rendered = (target / "energy" / "SKILL.md").read_text()
    assert rendered == "URL=https://v1.example.com/v2\n"


# ---------------------------------------------------------------------------
# 5. Publish — copy local skills back into a registered source repo.
# ---------------------------------------------------------------------------


def test_publish_copies_local_skill_and_commits(run, home, fake_user_home, tmp_path):
    """A user with a polished local skill in ~/.claude/skills/ should be
    able to publish it into a registered source repo with one command —
    the publish must inject the SkillSyncer preamble and create a commit."""
    run("init", "--no-scan", check=True)

    # Local skill in the fake user home (Path.home() reads $HOME on Linux/Mac).
    skill_dir = fake_user_home / ".claude" / "skills" / "energy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Energy\n\nUse the gateway.\n", encoding="utf-8")

    source = init_git_repo(tmp_path / "source-repo")
    write_config(home, {
        "sources": [{"name": "yc", "url": "git@example.com:me/yc.git",
                     "path": str(source)}],
        "targets": [],
    })

    result = run("publish", "--all", check=True)

    published = source / "energy" / "SKILL.md"
    assert published.is_file(), result.output
    text = published.read_text()
    assert "skillsyncer:require" in text
    assert "Use the gateway" in text

    log = git("log", "--oneline", cwd=source).stdout
    assert "Publish 1 skill" in log


def test_publish_blocks_when_skill_contains_hardcoded_secret(
    run, home, fake_user_home, tmp_path
):
    """Pre-flight scan must block publish when a skill contains what
    looks like a hardcoded secret. No commit should be created."""
    run("init", "--no-scan", check=True)

    skill_dir = fake_user_home / ".claude" / "skills" / "leaky"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "token sk-abcdefghij1234567890\n", encoding="utf-8"
    )

    source = init_git_repo(tmp_path / "source-repo")
    write_config(home, {
        "sources": [{"name": "yc", "path": str(source)}],
        "targets": [],
    })

    result = run("publish", "--all")
    assert result.returncode == 1
    assert "pre-flight scan" in result.stderr.lower()

    # No commits should have been created.
    log = git("log", "--oneline", "--all", cwd=source)
    assert log.stdout.strip() == ""


# ---------------------------------------------------------------------------
# 6. Secrets — set and list, with mandatory value redaction.
# ---------------------------------------------------------------------------


def test_secret_set_and_list_never_prints_value(run, home):
    run("init", "--no-scan", check=True)
    run("secret-set", "MY_API_KEY", "super-secret-do-not-leak", check=True)

    result = run("secret-list", check=True)
    assert "MY_API_KEY" in result.stdout
    # Values must NEVER appear in any output stream.
    assert "super-secret-do-not-leak" not in result.stdout
    assert "super-secret-do-not-leak" not in result.stderr

    # The value lives in identity.yaml on disk (that file is private).
    identity = yaml.safe_load((home / "identity.yaml").read_text())
    assert identity["secrets"]["MY_API_KEY"] == "super-secret-do-not-leak"


# ---------------------------------------------------------------------------
# 7. Scan — detect secrets in arbitrary files.
# ---------------------------------------------------------------------------


def test_scan_clean_file_exit_zero(run, home, tmp_path):
    run("init", "--no-scan", check=True)
    f = tmp_path / "clean.md"
    f.write_text("This file contains no secrets at all.\n", encoding="utf-8")
    result = run("scan", "--path", str(f))
    assert result.returncode == 0
    assert "No secrets detected" in result.stdout


def test_scan_finds_secret_with_json_output(run, home, tmp_path):
    run("init", "--no-scan", check=True)
    f = tmp_path / "leak.md"
    f.write_text("token sk-abcdefghij1234567890\n", encoding="utf-8")

    result = run("scan", "--path", str(f), "--format", "json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert isinstance(payload["detections"], list)
    assert len(payload["detections"]) >= 1
    detection = payload["detections"][0]
    assert "line" in detection
    assert "pattern_label" in detection


# ---------------------------------------------------------------------------
# 8. Guard — auto-fix a known secret that's already staged in a real repo.
# ---------------------------------------------------------------------------


def test_guard_fix_replaces_known_secret_in_staged_file(run, home, tmp_path):
    """Real workflow: user accidentally stages a file that contains the
    literal value of a secret they've registered with secret-set. ``guard
    --fix`` must rewrite it to the ``${{KEY}}`` placeholder so the push
    can proceed safely."""
    run("init", "--no-scan", check=True)
    repo = init_git_repo(tmp_path / "user-repo")

    leaky = repo / "SKILL.md"
    leaky.write_text("k=sk-abcdefghij1234567890\n", encoding="utf-8")
    git("add", "SKILL.md", cwd=repo)

    run("secret-set", "API_KEY", "sk-abcdefghij1234567890", check=True)

    result = run("guard", "--fix", "--path", str(repo), check=True)
    assert leaky.read_text() == "k=${{API_KEY}}\n"
    assert "fixed" in result.output.lower()


# ---------------------------------------------------------------------------
# 9. Hooks — install puts an executable, marker-blocked pre-push in place.
# ---------------------------------------------------------------------------


def test_hooks_install_uninstall_round_trip(run, home, tmp_path):
    run("init", "--no-scan", check=True)
    repo = init_git_repo(tmp_path / "repo")

    # Status before install.
    result = run("hooks", "status", "--path", str(repo), check=True)
    assert "not installed" in result.stdout

    run("hooks", "install", "--path", str(repo), check=True)
    pre_push = repo / ".git" / "hooks" / "pre-push"
    assert pre_push.is_file()
    assert "[skillsyncer:hook]" in pre_push.read_text()
    # Must be executable so git will run it.
    mode = pre_push.stat().st_mode
    assert mode & stat.S_IXUSR

    result = run("hooks", "status", "--path", str(repo), check=True)
    assert "installed" in result.stdout
    assert "not installed" not in result.stdout

    run("hooks", "uninstall", "--path", str(repo), check=True)
    assert not pre_push.exists()


# ---------------------------------------------------------------------------
# 10. Pre-push hook script — actually run it like git would.
# ---------------------------------------------------------------------------


def test_pre_push_hook_auto_fixes_known_secret(
    run, cli_argv, home, fake_user_home, tmp_path
):
    """Drive the *real* pre-push hook script end-to-end.

    The hook is what gates a real ``git push`` in production, so it has
    to behave correctly when invoked the way git invokes it: as a
    standalone shell script in the repo's hooks dir, with no flags. The
    hook resolves ``skillsyncer`` from ``$PATH`` — we make sure the
    directory of the test's binary is on PATH so the hook is *not* the
    silent no-op fallback path.
    """
    import os
    import shutil
    import subprocess

    run("init", "--no-scan", check=True)
    run("secret-set", "API_KEY", "sk-abcdefghij1234567890", check=True)

    repo = init_git_repo(tmp_path / "repo")
    run("hooks", "install", "--path", str(repo), check=True)

    leaky = repo / "SKILL.md"
    leaky.write_text("k=sk-abcdefghij1234567890\n", encoding="utf-8")
    git("add", "SKILL.md", cwd=repo)

    # The hook does ``command -v skillsyncer`` and silently exits if
    # missing. When tests run against a venv (or pipx) install, that
    # bin dir might not be on the inherited PATH — prepend it so the
    # hook actually runs the real CLI rather than no-op'ing.
    if cli_argv[0] == shutil.which("skillsyncer"):
        bin_dir = str(Path(cli_argv[0]).parent)
    else:
        # Falling back to ``python -m skillsyncer.cli`` — the hook can't
        # invoke that, so create a tiny shim script and put it on PATH.
        shim_dir = tmp_path / "shim"
        shim_dir.mkdir()
        shim = shim_dir / "skillsyncer"
        shim.write_text(
            "#!/bin/sh\nexec " + " ".join(repr(a) for a in cli_argv) + ' "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
        bin_dir = str(shim_dir)

    hook_path = repo / ".git" / "hooks" / "pre-push"
    env = os.environ.copy()
    env.update({
        "HOME": str(fake_user_home),
        "SKILLSYNCER_HOME": str(home),
        "NO_COLOR": "1",
        "PATH": bin_dir + os.pathsep + env.get("PATH", ""),
    })
    proc = subprocess.run(
        ["bash", str(hook_path)],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # Hook should have re-staged the fixed file and exited cleanly.
    assert proc.returncode == 0, proc.stderr
    assert leaky.read_text() == "k=${{API_KEY}}\n"
    diff = git("diff", "--cached", "SKILL.md", cwd=repo).stdout
    assert "${{API_KEY}}" in diff


# ---------------------------------------------------------------------------
# 11. skills / status — discovery against a fake user home.
# ---------------------------------------------------------------------------


def test_skills_lists_skills_from_fake_user_home(run, home, fake_user_home):
    skill_dir = fake_user_home / ".claude" / "skills" / "energy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Energy\n", encoding="utf-8")

    result = run("skills", check=True)
    assert "energy" in result.stdout
    assert "claude-code" in result.stdout


def test_skills_json_returns_a_list(run, home, fake_user_home):
    skill_dir = fake_user_home / ".cursor" / "skills" / "alerts"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Alerts\n", encoding="utf-8")

    result = run("skills", "--json", check=True)
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    names = {s["name"] for s in payload}
    assert "alerts" in names


def test_status_smoke_runs_against_a_real_source(run, home, tmp_path):
    run("init", "--no-scan", check=True)
    src = tmp_path / "src"
    write_skill(
        src, "energy", "k=${{K}}",
        manifest={"name": "energy",
                  "requires": {"secrets": [{"name": "K", "description": "the key"}]}},
    )
    write_config(home, {
        "sources": [{"name": "s", "path": str(src)}],
        "targets": [],
    })

    result = run("status", check=True)
    assert "energy" in result.stdout
    assert "K" in result.stdout


# ---------------------------------------------------------------------------
# 12. Doctor — diagnostics surface a missing home as a real error.
# ---------------------------------------------------------------------------


def test_doctor_reports_missing_home(run, tmp_path):
    """If $SKILLSYNCER_HOME points at a non-existent dir, doctor must
    exit non-zero and tell the user to run ``init``."""
    missing = tmp_path / "definitely-not-here"
    # Override SKILLSYNCER_HOME in the env to a path that doesn't exist;
    # the `run` fixture already sets one, so we override it here.
    result = run(
        "doctor",
        env={"SKILLSYNCER_HOME": str(missing)},
    )
    assert result.returncode == 1
    assert "does not exist" in result.stdout
    assert "init" in result.stdout
