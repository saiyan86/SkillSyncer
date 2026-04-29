"""Tests for skillsyncer.git helpers + secret-redaction guarantees."""

from __future__ import annotations

import yaml

from skillsyncer.cli import main as cli_main
from skillsyncer.git import (
    ENV_VAR,
    build_git_argv,
    get_extra_header,
    redact,
)


# ---------------------------------------------------------------------------
# build_git_argv: construction
# ---------------------------------------------------------------------------


def test_build_git_argv_no_header_passthrough():
    assert build_git_argv(["status"]) == ["git", "status"]


def test_build_git_argv_explicit_header_inserted_before_subcommand():
    cmd = build_git_argv(
        ["clone", "--quiet", "https://example/x.git", "/tmp/x"],
        extra_header="Authorization: Basic abc",
    )
    assert cmd[:3] == ["git", "-c", "http.extraHeader=Authorization: Basic abc"]
    assert cmd[3:] == ["clone", "--quiet", "https://example/x.git", "/tmp/x"]


def test_build_git_argv_falls_back_to_env(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "Authorization: Basic envvalue")
    cmd = build_git_argv(["pull", "--ff-only"])
    assert cmd[1:3] == ["-c", "http.extraHeader=Authorization: Basic envvalue"]


def test_build_git_argv_explicit_beats_env(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "Authorization: Basic envvalue")
    cmd = build_git_argv(["pull"], extra_header="Authorization: Basic explicit")
    assert "http.extraHeader=Authorization: Basic explicit" in cmd
    assert "http.extraHeader=Authorization: Basic envvalue" not in cmd


def test_get_extra_header_empty_treated_as_none(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "")
    assert get_extra_header(None) is None
    assert get_extra_header("") is None


def test_redact_replaces_header_value(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "Authorization: Basic SECRETVALUE==")
    out = redact(["error: bad creds Authorization: Basic SECRETVALUE== boom"])
    assert "SECRETVALUE" not in out[0]
    assert "<redacted>" in out[0]


# ---------------------------------------------------------------------------
# CLI integration: secret never lands in config / state / report output
# ---------------------------------------------------------------------------


def _invoke(*argv: str) -> int:
    try:
        return cli_main(list(argv)) or 0
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 0 if exc.code in (None, "") else 1


def test_add_with_extra_header_records_only_boolean_no_secret(
    tmp_path, monkeypatch, capsys,
):
    """`skillsyncer add --no-clone --git-extra-header ...` must:
      - persist requires_auth=true
      - NEVER persist the header value to config
      - NEVER print the header value to stdout/stderr
    """
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("SKILLSYNCER_HOME", str(h))

    # Local source dir for --no-clone path (avoids hitting the network).
    src = tmp_path / "fake-source"
    src.mkdir()

    secret = "Authorization: Basic SUPERSECRETBASE64=="
    rc = _invoke(
        "add",
        "--name", "private-skills",
        "--no-clone",
        "--git-extra-header", secret,
        str(src),
    )
    assert rc == 0, capsys.readouterr().err

    config_yaml = (h / "config.yaml").read_text()
    assert "SUPERSECRETBASE64" not in config_yaml
    assert "extraHeader" not in config_yaml
    assert "Authorization" not in config_yaml

    config = yaml.safe_load(config_yaml)
    src_entry = config["sources"][0]
    assert src_entry["name"] == "private-skills"

    # Captured output must not contain the secret either.
    cap = capsys.readouterr()
    assert "SUPERSECRETBASE64" not in (cap.out + cap.err)


def test_add_with_extra_header_via_env_does_not_leak(tmp_path, monkeypatch, capsys):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("SKILLSYNCER_HOME", str(h))
    monkeypatch.setenv(ENV_VAR, "Authorization: Basic ENVSECRETBLOB==")

    src = tmp_path / "src"
    src.mkdir()
    rc = _invoke("add", "--name", "p", "--no-clone", str(src))
    assert rc == 0

    text = (h / "config.yaml").read_text()
    assert "ENVSECRETBLOB" not in text
    cap = capsys.readouterr()
    assert "ENVSECRETBLOB" not in (cap.out + cap.err)


def test_add_records_requires_auth_flag(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("SKILLSYNCER_HOME", str(h))

    src = tmp_path / "src"
    src.mkdir()
    rc = _invoke(
        "add",
        "--name", "p",
        "--no-clone",
        "--git-extra-header", "Authorization: Basic XYZ",
        str(src),
    )
    assert rc == 0
    cfg = yaml.safe_load((h / "config.yaml").read_text())
    assert cfg["sources"][0].get("requires_auth") is True


def test_add_clone_invokes_git_with_extra_header(tmp_path, monkeypatch, capsys):
    """Spy on subprocess.run to verify the argv passed to git clone
    includes ``-c http.extraHeader=...``."""
    import skillsyncer.cli as cli

    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("SKILLSYNCER_HOME", str(h))

    captured: list[list[str]] = []

    class _FakeProc:
        stdout = ""
        stderr = ""
        returncode = 0

    def _fake_run(cmd, *a, **kw):
        captured.append(list(cmd))
        # Pretend the clone produced a directory with a .git subdir, so
        # hooks.install_hooks doesn't reject it.
        if cmd[:2] == ["git"] and "clone" in cmd:
            target = cmd[-1]
            from pathlib import Path as _P
            (_P(target) / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
        return _FakeProc()

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)

    rc = _invoke(
        "add",
        "--name", "private",
        "--git-extra-header", "Authorization: Basic ABC123",
        "https://example.test/private/skills.git",
    )
    assert rc == 0

    # First run() is the git clone (target dir doesn't exist yet).
    clone_call = next(
        c for c in captured if "clone" in c
    )
    assert clone_call[0] == "git"
    assert "-c" in clone_call
    idx = clone_call.index("-c")
    assert clone_call[idx + 1] == "http.extraHeader=Authorization: Basic ABC123"

    # Output must not surface the secret.
    cap = capsys.readouterr()
    assert "ABC123" not in (cap.out + cap.err)
