"""Fixtures and helpers for end-to-end tests.

E2E tests exercise SkillSyncer through the real ``skillsyncer`` console
script (or, as a fallback, ``python -m skillsyncer.cli``) so they validate
the full process boundary: argparse, entry-point wiring, exit codes,
stdout/stderr, and on-disk side effects.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import pytest


@dataclass
class CLIResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def _resolve_cli() -> list[str]:
    """Find the skillsyncer entry-point.

    Prefers the installed console script (validates packaging); falls
    back to ``python -m skillsyncer.cli`` so tests still run from a
    plain source checkout without an editable install.
    """
    if exe := shutil.which("skillsyncer"):
        return [exe]
    return [sys.executable, "-m", "skillsyncer.cli"]


@pytest.fixture(scope="session")
def cli_argv() -> list[str]:
    return _resolve_cli()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Isolated SKILLSYNCER_HOME for one test."""
    h = tmp_path / "skillsyncer-home"
    h.mkdir()
    return h


@pytest.fixture
def fake_user_home(tmp_path: Path) -> Path:
    """Isolated $HOME for one test — keeps Path.home() lookups (used
    by the discoverer to find ~/.claude, ~/.cursor, etc.) sandboxed."""
    h = tmp_path / "fake-user-home"
    h.mkdir()
    return h


@pytest.fixture
def run(cli_argv, home, fake_user_home, tmp_path):
    """Run the skillsyncer CLI in an isolated environment.

    Returns a callable: ``run(*args, cwd=..., env=..., input=..., check=...)``.
    By default we set both ``SKILLSYNCER_HOME`` and ``HOME`` to test-local
    dirs so nothing the CLI does (writing config, scanning agent dirs)
    can leak into the real user's machine.
    """

    def _run(
        *args: str,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        check: bool = False,
        timeout: int = 60,
    ) -> CLIResult:
        full_env = os.environ.copy()
        full_env["SKILLSYNCER_HOME"] = str(home)
        full_env["HOME"] = str(fake_user_home)
        # Disable color so assertions can match plain strings.
        full_env["NO_COLOR"] = "1"
        # Be predictable about Python output.
        full_env["PYTHONIOENCODING"] = "utf-8"
        if env:
            full_env.update(env)

        proc = subprocess.run(
            [*cli_argv, *args],
            cwd=str(cwd) if cwd else None,
            env=full_env,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        result = CLIResult(proc.returncode, proc.stdout, proc.stderr)
        if check and result.returncode != 0:
            raise AssertionError(
                f"command failed (rc={result.returncode}): "
                f"skillsyncer {' '.join(args)}\n"
                f"--- stdout ---\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}"
            )
        return result

    return _run


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command with deterministic identity and no gpg signing.

    We force ``commit.gpgsign=false`` and a fixed author/committer
    via ``-c`` so tests work on machines with global gpg-signing
    enabled (CI usually doesn't, dev machines often do).
    """
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    })
    return subprocess.run(
        ["git",
         "-c", "commit.gpgsign=false",
         "-c", "tag.gpgsign=false",
         "-c", "init.defaultBranch=main",
         *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def init_git_repo(path: Path) -> Path:
    """Initialize a git repo at *path* with a stable user identity."""
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "test@example.com", cwd=path)
    git("config", "user.name", "Test", cwd=path)
    git("config", "commit.gpgsign", "false", cwd=path)
    git("config", "tag.gpgsign", "false", cwd=path)
    return path


def write_skill(
    repo: Path,
    name: str,
    body: str,
    *,
    manifest: Mapping | None = None,
) -> Path:
    """Create a skill directory under *repo* with SKILL.md and optional manifest."""
    import yaml

    skill_dir = repo / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    if manifest is not None:
        (skill_dir / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
    return skill_dir


def write_config(home: Path, config: Mapping) -> Path:
    """Write a SkillSyncer config.yaml into the test home."""
    import yaml

    cfg = home / "config.yaml"
    cfg.write_text(yaml.safe_dump(dict(config), sort_keys=False), encoding="utf-8")
    return cfg
