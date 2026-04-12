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

# env-var name fragments we treat as credential-ish. Includes the
# common AI provider prefixes so e.g. ANTHROPIC_BASE_URL still
# matches even though it doesn't end in KEY/TOKEN/etc.
_CRED_NAME_PATTERN = re.compile(
    r"(KEY|TOKEN|SECRET|API|URL|WEBHOOK|PASSWORD|PASSWD|CREDENTIAL|ENDPOINT|"
    r"ANTHROPIC|OPENAI|GOOGLE|GEMINI|GROQ|MISTRAL|XAI|DEEPSEEK|HUGGINGFACE|"
    r"REPLICATE|PERPLEXITY|COHERE|TOGETHER|FIREWORKS|OPENROUTER|LITELLM|"
    r"BEDROCK|VERTEX)",
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

# Each agent: ordered list of candidate skill dirs. The first one
# that exists wins. If none exist, the first entry is reported as
# the canonical (not-found) path.
_AGENT_CANDIDATES = [
    {"name": "claude-code",     "paths": ["{home}/.claude/skills"]},
    {"name": "claude-cowork",   "paths": ["{home}/.claude-cowork/skills",
                                          "{home}/.config/claude-cowork/skills",
                                          "{home}/.claude/cowork/skills"]},
    {"name": "cursor",          "paths": ["{home}/.cursor/skills"]},
    {"name": "windsurf",        "paths": ["{home}/.windsurf/skills"]},
    {"name": "gemini",          "paths": ["{home}/.gemini/skills"]},
    {"name": "codex",           "paths": ["{home}/.codex/skills"]},
    {"name": "openclaw",        "paths": ["{home}/.openclaw/skills",
                                          "{home}/.openclaw/agents",
                                          "{home}/openclaw/skills"]},
    {"name": "hermes",          "paths": ["{home}/.hermes/skills",
                                          "{home}/.hermes/agents",
                                          "{home}/.config/hermes/skills"]},
    {"name": "github-copilot",  "paths": ["{home}/.config/github-copilot"]},
]


def discover(
    home: str | Path | None = None,
    cwd: str | Path | None = None,
    env: dict | None = None,
    scan_credentials: bool = True,
) -> dict:
    """Run an environment scan and return a proposal dict.

    When ``scan_credentials`` is False, the credential scan is skipped
    entirely (no files are read for credential extraction). The
    proposal still contains a ``credential_scan_plan`` so callers can
    show the user what *would* be scanned and ask for consent.
    """
    home_path = Path(home).expanduser() if home else Path.home()
    cwd_path = Path(cwd) if cwd else Path.cwd()
    env_map = os.environ if env is None else env

    plan = credential_scan_locations(home_path, cwd_path, env_map)

    return {
        "agents": _discover_agents(home_path),
        "existing_skills": _discover_existing_skills(home_path),
        "credentials": (
            _discover_credentials(home_path, cwd_path, env_map)
            if scan_credentials else []
        ),
        "credential_scan_plan": plan,
        "credential_scan_performed": scan_credentials,
        "git": _discover_git(cwd_path),
    }


def credential_scan_locations(
    home: Path,
    cwd: Path,
    env: dict | None = None,
) -> list[dict]:
    """Return the list of locations the credential scan would read.

    Each entry: ``{path, display, exists, kind}``. ``kind`` is one of
    ``project``, ``shell``, ``home``, ``ai-tool``. Used by the
    consent screen to tell the user *exactly* what will be touched.
    """
    env_map = os.environ if env is None else env
    plan: list[dict] = []

    def _add(p: Path, kind: str, display: str | None = None) -> None:
        plan.append({
            "path": str(p),
            "display": display or _short(p, home),
            "exists": _is_user_file(p) or _is_dir_safe(p),
            "kind": kind,
        })

    # Project (cwd)
    for name in (".env", ".env.local", ".env.production"):
        _add(cwd / name, "project")
    for name in ("docker-compose.yml", "docker-compose.yaml", "docker-compose.override.yml"):
        _add(cwd / name, "project")

    # User home
    for name in (".env", ".env.local"):
        _add(home / name, "home")
    _add(home / ".kube" / "config", "home")

    # AI tool config dirs (system-wide sweep)
    for d in _ai_tool_dirs(home):
        _add(d, "ai-tool")

    # Shell environment — represented as a single entry, since we
    # never scan individual vars; we just filter os.environ.
    cred_var_count = sum(
        1 for k in env_map
        if k not in _SYSTEM_ENV and _looks_credential(k)
    )
    plan.append({
        "path": "(shell environment)",
        "display": f"$ENV (matched {cred_var_count} credential-shaped vars)",
        "exists": cred_var_count > 0,
        "kind": "shell",
    })

    return plan


def _short(p: Path, home: Path) -> str:
    try:
        return "~/" + str(p.relative_to(home))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------
# agents + existing skills
# ---------------------------------------------------------------------------


def _resolve_agent_path(agent: dict, home: Path) -> tuple[Path, bool]:
    """Pick the first existing candidate dir for an agent.

    Returns ``(path, found)``. If none exist, the first candidate is
    returned with ``found=False`` so callers still have something to
    show in the proposal.
    """
    candidates = [Path(p.format(home=str(home))) for p in agent["paths"]]
    for path in candidates:
        if path.exists():
            return path, True
    # Fallback: use the first candidate; mark "found" if its parent
    # exists, which means the agent itself is installed but has no
    # skills/ subdir yet.
    first = candidates[0]
    return first, first.parent.exists()


def _discover_agents(home: Path) -> list[dict]:
    out: list[dict] = []
    for agent in _AGENT_CANDIDATES:
        path, found = _resolve_agent_path(agent, home)
        out.append({
            "name": agent["name"],
            "path": str(path),
            "found": found,
        })
    return out


def _discover_existing_skills(home: Path) -> list[dict]:
    """Find depth-1 skills under each detected agent dir.

    Convention: ``<agent_dir>/<skill_name>/SKILL.md``. Plugin bundles
    that nest skills more deeply (e.g.
    ``<agent_dir>/<plugin>/<skill>/SKILL.md``) are intentionally
    skipped here — listing them as bare names produces hundreds of
    duplicates and confuses the proposal output.
    """
    skills: list[dict] = []
    seen_paths: set[str] = set()
    for agent in _discover_agents(home):
        agent_dir = Path(agent["path"])
        if not agent_dir.is_dir():
            continue
        for child in sorted(agent_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            md = child / "SKILL.md"
            if not md.is_file():
                continue
            md_abs = str(md.resolve())
            if md_abs in seen_paths:
                continue
            seen_paths.add(md_abs)
            try:
                content = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            skills.append({
                "name": child.name,
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

    # cwd + $HOME .env-style files (project + user-shell creds)
    base_candidates = [
        cwd / ".env",
        cwd / ".env.local",
        cwd / ".env.production",
        home / ".env",
        home / ".env.local",
    ]
    for path in base_candidates:
        if not _is_user_file(path):
            continue
        try:
            display = str(path.relative_to(home))
        except ValueError:
            display = path.name
        for k, v in _parse_env_file(path):
            _add(k, v, display, str(path))

    # System-wide sweep across well-known AI / agent tool config dirs.
    # Each dir is scanned for known credential file names; we also walk
    # one level deep so per-profile configs are picked up.
    for tool_dir in _ai_tool_dirs(home):
        for k, v, source, path in _scan_tool_dir(tool_dir, home):
            _add(k, v, source, path)

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


# Well-known AI / agent tool config directories. We scan these
# system-wide for credential files. New tools can be added freely;
# misses are silent and cheap.
_AI_TOOL_HOME_DIRS = [
    # Direct ~ subdirs
    ".claude", ".claude-cowork", ".cursor", ".codex", ".openclaw",
    ".hermes", ".gemini", ".windsurf", ".zed", ".fleet",
    ".continue", ".aider", ".cody", ".tabnine",
    ".anthropic", ".openai", ".litellm", ".llm",
    # XDG ~/.config
    ".config/anthropic", ".config/openai",
    ".config/claude", ".config/claude-cowork",
    ".config/cursor", ".config/codex",
    ".config/openclaw", ".config/hermes",
    ".config/gemini", ".config/windsurf", ".config/zed",
    ".config/continue", ".config/aider", ".config/cody",
    ".config/litellm", ".config/openrouter", ".config/fabric",
    ".config/llm", ".config/github-copilot",
    # macOS Application Support
    "Library/Application Support/Claude",
    "Library/Application Support/Anthropic",
    "Library/Application Support/Cursor",
    "Library/Application Support/Codex",
    "Library/Application Support/OpenClaw",
    "Library/Application Support/Continue",
    "Library/Application Support/Aider",
]

# Filenames inside any AI tool dir we'll parse for credentials.
# Includes MCP-server config conventions used by Claude Desktop,
# Cursor, OpenClaw, Continue.dev, etc.
_CRED_FILE_NAMES = {
    ".env", ".env.local", ".env.production",
    "config.env", "auth.env",
    "credentials", "credentials.json",
    "credentials.yaml", "credentials.yml",
    "secrets.json", "secrets.yaml", "secrets.yml",
    "config.json", "config.yaml", "config.yml",
    "settings.json", "auth.json", ".auth",
    # MCP / tool server configs
    "mcp.json", "mcp_servers.json", "mcp-servers.json",
    "claude_desktop_config.json",
    "servers.json", "tools.json", "extensions.json",
    "agent.yaml", "agent.yml", "agent.json",
}

# Subdirectory names that are noise — skip them when walking.
_WALK_SKIP_DIRS = {
    "node_modules", "venv", ".venv", "__pycache__", ".git",
    "dist", "build", ".cache", "logs", "Cache", "GPUCache",
    "Code Cache", "blob_storage", "tmp", "temp",
}

_MAX_WALK_DEPTH = 3

_AGENT_CRED_KEYS = ("secrets", "credentials", "env", "environment", "api_keys", "keys")

# Containers whose entries each have a nested env block. This is the
# MCP / tool-server convention shared by Claude Desktop, OpenClaw,
# Cursor, Continue, Windsurf, etc.
_AGENT_SERVER_KEYS = ("mcpServers", "mcp_servers", "servers", "tools", "extensions")


def _is_cred_filename(name: str) -> bool:
    if name in _CRED_FILE_NAMES:
        return True
    lower = name.lower()
    return lower.endswith(".env") or lower.endswith(".env.local")


def _ai_tool_dirs(home: Path) -> list[Path]:
    return [home / rel for rel in _AI_TOOL_HOME_DIRS]


def _scan_tool_dir(tool_dir: Path, home: Path):
    """Walk ``tool_dir`` (depth ≤ 3) and yield credentials from any
    known cred file. Bounded depth + filename allowlist + noise-dir
    skip keep this fast even on tools with deep cache trees."""
    if not _is_dir_safe(tool_dir):
        return

    for path in _walk_for_cred_files(tool_dir):
        try:
            display = str(path.relative_to(home))
        except ValueError:
            display = path.name

        lower = path.name.lower()
        if lower.endswith(".json") or lower.endswith((".yaml", ".yml")):
            pairs = _parse_agent_config(path)
        else:
            # .env, .env.local, credentials, settings, auth — line-based
            pairs = _parse_env_file(path)

        for k, v in pairs:
            yield k, v, display, str(path)


def _walk_for_cred_files(root: Path):
    """Yield ``Path`` objects for files matching ``_is_cred_filename``,
    walking ``root`` to a maximum depth and skipping noise dirs.

    Hidden subdirectories are skipped *below* the root (the root
    itself is allowed to be hidden — that's how we get into
    ``~/.openclaw`` in the first place).
    """
    def _walk(d: Path, depth: int):
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name in _WALK_SKIP_DIRS:
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if depth >= _MAX_WALK_DEPTH:
                    continue
                if entry.name.startswith(".") and depth > 0:
                    continue
                yield from _walk(entry, depth + 1)
                continue
            if _is_cred_filename(entry.name):
                yield entry

    yield from _walk(root, 0)


def _is_dir_safe(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def _parse_agent_config(path: Path) -> list[tuple[str, str]]:
    """Pull credential-shaped key/value pairs out of an agent config file.

    Looks for a top-level dict whose key is one of ``secrets``,
    ``credentials``, ``env``, ``environment``, ``api_keys``, ``keys``,
    and treats the contents as flat ``KEY: value`` pairs. Also pulls
    any top-level keys whose names match the credential pattern.
    """
    out: list[tuple[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    if path.suffix == ".json":
        try:
            import json as _json
            data = _json.loads(text)
        except (ValueError, OSError):
            return out
    else:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            return out

    if not isinstance(data, dict):
        return out

    # Top-level keys that look credential-shaped.
    for k, v in data.items():
        if isinstance(v, (str, int, float)) and _looks_credential(str(k)):
            out.append((str(k), str(v)))

    # Common credential containers (flat dict or list of KEY=VAL).
    for container in _AGENT_CRED_KEYS:
        section = data.get(container)
        if isinstance(section, dict):
            for k, v in section.items():
                if v is None:
                    continue
                if isinstance(v, (str, int, float, bool)):
                    out.append((str(k), str(v)))
        elif isinstance(section, list):
            for entry in section:
                if isinstance(entry, str) and "=" in entry:
                    k, v = entry.split("=", 1)
                    out.append((k.strip(), v.strip()))
                elif isinstance(entry, dict) and "name" in entry:
                    val = entry.get("value")
                    if val is not None:
                        out.append((str(entry["name"]), str(val)))

    # MCP / tool-server containers — each child has its own ``env`` /
    # ``environment`` block. This is the convention used by Claude
    # Desktop, OpenClaw, Cursor, Continue, Windsurf, etc.
    for container in _AGENT_SERVER_KEYS:
        servers = data.get(container)
        if not isinstance(servers, dict):
            continue
        for server_def in servers.values():
            if not isinstance(server_def, dict):
                continue
            env_block = server_def.get("env") or server_def.get("environment")
            if isinstance(env_block, dict):
                for k, v in env_block.items():
                    if v is None:
                        continue
                    if isinstance(v, (str, int, float, bool)):
                        out.append((str(k), str(v)))
            elif isinstance(env_block, list):
                for entry in env_block:
                    if isinstance(entry, str) and "=" in entry:
                        k, v = entry.split("=", 1)
                        out.append((k.strip(), v.strip()))
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
