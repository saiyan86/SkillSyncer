"""Environment scanning for ``skillsyncer init``.

Builds a single ``DiscoverResult`` dict that ``init`` (or the operator
agent) can present back to the user as a one-shot proposal.

Hard rules:
- Never log or print credential values from this module.
- Only inspect files the current user can read.
- Skip system env vars (PATH, HOME, etc.).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

from .scanner import scan_content

PLACEHOLDER_RE = re.compile(r"\$\{\{[A-Z_][A-Z0-9_]*\}\}")

# These are the only env-var name fragments we treat as credential-ish.
_CRED_NAME_PATTERN = re.compile(
    r"(KEY|TOKEN|SECRET|API|URL|WEBHOOK|PASSWORD|PASSWD|CREDENTIAL|ENDPOINT)",
    re.IGNORECASE,
)

# System env vars that we always skip even if they match _CRED_NAME_PATTERN.
_SYSTEM_ENV = {
    "PATH", "HOME", "SHELL", "TERM", "USER", "PWD", "OLDPWD", "TMPDIR",
    "DISPLAY", "LANG", "LC_ALL", "LC_CTYPE", "EDITOR", "VISUAL",
    "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "SSH_AUTH_SOCK",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "LOGNAME", "MAIL", "HOSTNAME",
}

_AGENT_CANDIDATES = [
    ("claude-code", "{home}/.claude/skills"),
    ("cursor", "{home}/.cursor/skills"),
    ("windsurf", "{home}/.windsurf/skills"),
    ("gemini", "{home}/.gemini/skills"),
    ("codex", "{home}/.codex/skills"),
    ("openclaw", "{home}/openclaw/skills"),
    ("github-copilot", "{home}/.config/github-copilot"),
]


def discover(
    home: str | Path | None = None,
    cwd: str | Path | None = None,
    env: dict | None = None,
) -> dict:
    """Run a full environment scan and return a proposal dict."""
    home_path = Path(home).expanduser() if home else Path.home()
    cwd_path = Path(cwd) if cwd else Path.cwd()
    env_map = os.environ if env is None else env

    return {
        "agents": _discover_agents(home_path),
        "existing_skills": _discover_existing_skills(home_path),
        "credentials": _discover_credentials(home_path, cwd_path, env_map),
        "git": _discover_git(cwd_path),
    }


# ---------------------------------------------------------------------------
# agents + existing skills
# ---------------------------------------------------------------------------


def _discover_agents(home: Path) -> list[dict]:
    out: list[dict] = []
    for name, template in _AGENT_CANDIDATES:
        path = Path(template.format(home=str(home)))
        out.append({
            "name": name,
            "path": str(path),
            "found": path.exists() or path.parent.exists(),
        })
    return out


def _discover_existing_skills(home: Path) -> list[dict]:
    skills: list[dict] = []
    for agent in _discover_agents(home):
        agent_dir = Path(agent["path"])
        if not agent_dir.is_dir():
            continue
        for md in sorted(agent_dir.rglob("SKILL.md")):
            try:
                content = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            skills.append({
                "name": md.parent.name,
                "agent": agent["name"],
                "path": str(md),
                "has_placeholders": bool(PLACEHOLDER_RE.search(content)),
                "has_hardcoded_secrets": bool(scan_content(content, {})),
            })
    return skills


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def _discover_credentials(home: Path, cwd: Path, env: dict) -> list[dict]:
    creds: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(key: str, value: str, source: str, path: str | None) -> None:
        if not value or not _looks_credential(key):
            return
        if (key, value) in seen:
            return
        seen.add((key, value))
        creds.append({"key": key, "value": value, "source": source, "path": path})

    # .env-style files
    env_candidates = [
        cwd / ".env",
        cwd / ".env.local",
        cwd / ".env.production",
        home / ".env",
        home / ".env.local",
    ]
    for path in env_candidates:
        if not _is_user_file(path):
            continue
        for k, v in _parse_env_file(path):
            _add(k, v, path.name, str(path))

    # docker-compose.yml — environment: sections
    for name in ("docker-compose.yml", "docker-compose.yaml", "docker-compose.override.yml"):
        path = cwd / name
        if not _is_user_file(path):
            continue
        for k, v in _parse_compose_env(path):
            _add(k, v, name, str(path))

    # ~/.kube/config — extract cluster server URLs
    kube = home / ".kube" / "config"
    if _is_user_file(kube):
        for k, v in _parse_kube_servers(kube):
            _add(k, v, ".kube/config", str(kube))

    # Shell environment
    for k, v in env.items():
        if k in _SYSTEM_ENV:
            continue
        _add(k, str(v), "shell environment", None)

    return creds


def _looks_credential(key: str) -> bool:
    return bool(_CRED_NAME_PATTERN.search(key))


def _is_user_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _parse_env_file(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out.append((key, value))
    return out


def _parse_compose_env(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return out
    services = (data.get("services") or {}) if isinstance(data, dict) else {}
    for service in services.values():
        if not isinstance(service, dict):
            continue
        env = service.get("environment")
        if isinstance(env, dict):
            for k, v in env.items():
                if v is not None:
                    out.append((str(k), str(v)))
        elif isinstance(env, list):
            for entry in env:
                if isinstance(entry, str) and "=" in entry:
                    k, v = entry.split("=", 1)
                    out.append((k.strip(), v.strip()))
    return out


def _parse_kube_servers(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return out
    for cluster in data.get("clusters") or []:
        info = cluster.get("cluster") or {}
        server = info.get("server")
        name = cluster.get("name") or "default"
        if server:
            normalized = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
            out.append((f"K8S_{normalized}_SERVER", server))
    return out


# ---------------------------------------------------------------------------
# git / gh
# ---------------------------------------------------------------------------


def _discover_git(cwd: Path) -> dict:
    info: dict = {
        "gh_authenticated": False,
        "current_project_remote": None,
        "suggested_repo_name": "agent-skills",
    }
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=5,
        )
        info["gh_authenticated"] = proc.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(cwd),
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            info["current_project_remote"] = proc.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    return info
