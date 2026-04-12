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
    {"name": "openclaw",        "paths": ["{home}/.openclaw/workspace/skills",
                                          "{home}/.openclaw/skills",
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
    for d in _ai_tool_dirs(home, env_map):
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

    # System-wide sweep across well-known AI / agent tool config dirs,
    # plus any locations the user has pointed at via env overrides
    # (OPENCLAW_HOME, HERMES_HOME, ...).
    for tool_dir in _ai_tool_dirs(home, env):
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
#
# Verified against the open-source projects themselves:
# - openclaw stores credentials in ~/.openclaw/openclaw.json and
#   ~/.openclaw/agents/<id>/agent/auth-profiles.json (legacy:
#   ~/.clawdbot/, clawdbot.json). See openclaw/openclaw repo
#   src/config/paths.ts and src/agents/auth-profiles/paths.ts.
# - hermes-agent stores credentials in ~/.hermes/config.yaml and
#   ~/.hermes/.env. See NousResearch/hermes-agent hermes_cli/config.py.
_AI_TOOL_HOME_DIRS = [
    # Direct ~ subdirs
    ".claude", ".claude-cowork", ".cursor", ".codex", ".openclaw",
    ".clawdbot",  # legacy openclaw (pre-rebrand)
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


def _env_override_dirs(env: dict) -> list[Path]:
    """Honor agent-specific HOME/STATE env overrides.

    Some agents let users move their state dir via an env var. We
    pick those up so the discoverer follows wherever the agent
    actually lives.
    """
    out: list[Path] = []
    for var in (
        "OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_AGENT_DIR",
        "PI_CODING_AGENT_DIR",  # legacy openclaw alias
        "HERMES_HOME", "CLAUDE_HOME", "CURSOR_HOME",
    ):
        raw = env.get(var)
        if raw and raw.strip():
            out.append(Path(raw).expanduser())
    return out

# Files we explicitly do NOT scan even if they're in an AI tool dir.
# Pure dependency-management noise — never holds credentials, often
# huge, would just slow the walker down.
_BORING_FILES = {
    "package.json", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "tsconfig.json", "tsconfig.base.json",
    "composer.json", "composer.lock",
    "Cargo.lock", "Pipfile.lock", "poetry.lock",
    "go.sum", "go.mod",
}

# A handful of fixed names we recognize even though they have no
# extension or use an unusual one. The recursive extractor handles
# all the structured-config cases — this list only catches plain
# secret files like ``credentials`` and ``.auth``.
_NO_EXT_CRED_NAMES = {"credentials", "auth", ".auth", "config.env", "auth.env"}

# Subdirectory names that are noise — skip them when walking.
# Mostly Electron / Chromium / VS Code caches that can contain
# tens of thousands of files we never want to read, plus anything
# that's clearly example/template/backup material rather than the
# user's live config.
_WALK_SKIP_DIRS = {
    # Build / language / dep junk
    "node_modules", "venv", ".venv", "__pycache__", ".git",
    "dist", "build", "target", "out", ".idea", ".vscode",
    # Logs / temp
    "logs", "tmp", "temp",
    # Generic caches
    ".cache", "Cache", "Cache_Data",
    # Electron / Chromium internals
    "GPUCache", "Code Cache", "Code Cache Js", "Code Cache Wasm",
    "blob_storage", "Crashpad", "Crash Reports", "DawnCache",
    "ShaderCache", "GrShaderCache", "VideoDecodeStats",
    "Service Worker", "IndexedDB", "Local Storage",
    "Session Storage", "WebStorage", "WebRTC Logs",
    # VS Code workspace state
    "workspaceStorage", "globalStorage", "History",
    # Examples / templates / fixtures — not user config
    "examples", "example", "templates", "template",
    "test", "tests", "fixtures", "samples", "sample",
    "tutorials", "tutorial", "demo", "demos",
    # Backups — stale, not the user's live state
    "backup", "backups", "bak", "old",
}

# Filename patterns that indicate protocol caches / session stores /
# backups. These hold credential-shaped fields with auto-generated
# key names (UUIDs, base64 blobs, ratchet roots, ...) that aren't
# the user-meaningful API keys we're trying to surface.
_NOISE_FILE_PATTERNS = [
    re.compile(r"cache", re.IGNORECASE),                       # m365-token-cache.json, *Cache*
    re.compile(r"^session[-_]", re.IGNORECASE),                # WhatsApp / Signal session
    re.compile(r"^pre[-_]?key[-_]", re.IGNORECASE),            # Signal pre-keys
    re.compile(r"^lid[-_]mapping[-_]", re.IGNORECASE),
    re.compile(r"^sender[-_]key[-_]", re.IGNORECASE),
    re.compile(r"^tctoken[-_]", re.IGNORECASE),
    re.compile(r"^app[-_]?state[-_]?sync[-_]", re.IGNORECASE),
    re.compile(r"\.bak(\.|$)", re.IGNORECASE),                 # *.bak, *.bak.1
]

_MAX_SYNTH_NAME_LEN = 64  # anything longer is almost certainly auto-generated

# Leaf key names that strongly indicate a credential value, regardless
# of the surrounding structure. The recursive extractor pulls every
# matching leaf and synthesizes a useful name from the path.
#
# Each suffix (Key / Token / Password / Secret) only matches when
# preceded by an *explicit credential-shaped prefix* — that's how
# we keep ``apiKey`` / ``appPassword`` / ``privateKey`` in while
# keeping protocol primitives like ``preKey`` / ``noiseKey`` /
# ``publicKey`` / ``registrationId`` out.
_LEAF_CRED_PATTERN = re.compile(
    r"^("
    # *Key — only with explicit credential prefixes
    r"(?:api|app|client|access|secret|master|admin|root|user|auth"
    r"|private|signing|encryption|rsa|aes|hmac|jwt)[_\-]?key|"
    # *Token — most cred tokens; bare ``token`` allowed
    r"(?:access|auth|bearer|id|refresh|session|api|client|app|oauth)?[_\-]?token|"
    # *Password / *Passwd — bare or with a common prefix
    r"(?:app|user|admin|root|db|database|master)?[_\-]?password|"
    r"(?:app|user|admin|root|db|database|master)?[_\-]?passwd|"
    # *Secret — bare or with a common prefix
    r"(?:app|client|api|master|jwt|oauth|consumer)?[_\-]?secret|"
    # Bearer credentials
    r"bearer|"
    # Webhooks
    r"webhook|webhook[_\-]?url|"
    # Connection strings / DSNs
    r"(?:db|database)?[_\-]?connection[_\-]?string|dsn|"
    # Pure ``credentials`` leaf (rare but happens)
    r"credentials?"
    r")$",
    re.IGNORECASE,
)

# Path components that introduce a NAMED ENTITY at the next position.
# When we see ``plugins.entries.brave.config.webSearch.apiKey``, the
# component immediately after ``entries`` (i.e. ``brave``) is the
# entity name we want to use when synthesizing the credential name.
_NAMED_ENTITY_CONTAINERS = {
    "entries", "providers", "servers", "accounts", "channels",
    "plugins", "tools", "skills", "agents", "models", "extensions",
    "mcpServers", "mcp_servers",
}

# Path components that are pure structure — never useful as the
# entity name when synthesizing.
_GENERIC_PATH_NAMES = {
    "entries", "providers", "servers", "accounts", "channels",
    "plugins", "tools", "skills", "agents", "models", "extensions",
    "mcpServers", "mcp_servers",
    "config", "configs", "value", "values", "env", "environment",
    "auth", "credentials", "secrets", "keys", "defaults",
    "default", "list", "items", "options", "settings", "properties",
    "data", "spec", "info", "params", "args", "arguments",
    "common", "global", "shared",
    "tokens", "identities", "profiles", "current", "latest",
    "main", "primary", "active",
}


def _is_scannable_filename(name: str) -> bool:
    """True if the file is worth opening for credential extraction.

    The strategy is "parse everything structured, except known noise".
    The leaf-key shape is what filters out false positives — not the
    filename — so we accept any ``.json`` / ``.yaml`` / ``.yml`` /
    ``.env*`` file plus a couple of fixed names.

    Excluded:
    - dependency-management lockfiles / manifests (``_BORING_FILES``)
    - protocol cache / session files (``_NOISE_FILE_PATTERNS``)
    """
    if name in _BORING_FILES:
        return False
    for pat in _NOISE_FILE_PATTERNS:
        if pat.search(name):
            return False
    if name in _NO_EXT_CRED_NAMES:
        return True
    lower = name.lower()
    if lower.endswith(".json") or lower.endswith((".yaml", ".yml")):
        return True
    if lower.endswith((".env", ".env.local", ".env.production")):
        return True
    # Catch *.env style names like "agent.env" / "production.env"
    if ".env" in lower and not lower.endswith((".envrc",)):
        return True
    return False


def _is_envvar_style(name: str) -> bool:
    """True for UPPER_SNAKE_CASE names like ``BRAVE_API_KEY``.

    These are already in the form a user / shell would expect, so the
    extractor uses them as-is instead of synthesizing from the path.
    """
    return bool(re.match(r"^[A-Z][A-Z0-9_]*$", name)) and "_" in name


def _camel_to_upper_snake(name: str) -> str:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"[-\s]+", "_", name)
    return name.upper()


def _sanitize_placeholder_name(name: str) -> str | None:
    """Force a synthesized name to match SkillSyncer's placeholder
    grammar: ``[A-Z_][A-Z0-9_]*``.

    Replaces any non-alphanumeric character with ``_``, collapses
    runs of underscores, strips leading underscores until the first
    char is a letter, and uppercases. Returns ``None`` if nothing
    valid remains (e.g. an all-numeric leaf).
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", name)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        return None
    if cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return cleaned.upper()


# Bare leaves that mean nothing on their own — without an entity
# prefix they're useless to the user (ACCESS_TOKEN / TOKEN / SECRET).
_LEAF_NEEDS_ENTITY = {
    "TOKEN", "ACCESS_TOKEN", "ID_TOKEN", "REFRESH_TOKEN",
    "SECRET", "PASSWORD", "PASSWD", "KEY", "API_KEY",
    "BEARER", "WEBHOOK", "DSN",
}


def _synth_cred_name(
    path: tuple[str, ...],
    file_stem: str | None = None,
) -> str | None:
    """Build an UPPER_SNAKE credential name from a path-in-tree.

    Strategy:
    1. Scan the path for one of ``_NAMED_ENTITY_CONTAINERS`` and
       use the next non-generic component as the entity. Deepest
       entity wins.
    2. Otherwise, use the nearest non-generic ancestor in the path.
    3. Otherwise, fall back to the file stem (filename without
       extension) so a bare ``token`` field in ``auth.json`` becomes
       ``AUTH_TOKEN`` instead of just ``TOKEN``.
    4. Sanitize against the placeholder grammar (``[A-Z_][A-Z0-9_]*``).
    5. Reject anything > 64 chars — that's an auto-generated cache key.
    """
    leaf = path[-1]
    parts = list(path[:-1])
    entity: str | None = None

    for i, part in enumerate(parts):
        if part in _NAMED_ENTITY_CONTAINERS and i + 1 < len(parts):
            candidate = parts[i + 1]
            if candidate not in _GENERIC_PATH_NAMES and not candidate.isdigit():
                entity = candidate  # deepest wins

    if entity is None:
        for part in reversed(parts):
            if part not in _GENERIC_PATH_NAMES and not part.isdigit():
                entity = part
                break

    raw = f"{entity}_{leaf}" if entity else leaf
    snake = _camel_to_upper_snake(raw)
    sanitized = _sanitize_placeholder_name(snake)

    # Bare-leaf rescue: if the result is just a generic word like
    # TOKEN / ACCESS_TOKEN / SECRET, prepend the file stem as entity.
    if sanitized in _LEAF_NEEDS_ENTITY and file_stem:
        stem_clean = _sanitize_placeholder_name(_camel_to_upper_snake(file_stem))
        if stem_clean and stem_clean not in {"AUTH", "CONFIG", "SECRETS", "CREDENTIALS"}:
            sanitized = f"{stem_clean}_{sanitized}"
        elif stem_clean:
            # auth.json → AUTH is generic, but pairing with the leaf is
            # still better than the bare leaf.
            sanitized = f"{stem_clean}_{sanitized}"

    if not sanitized or len(sanitized) > _MAX_SYNTH_NAME_LEN:
        return None
    return sanitized


def _walk_creds_in_obj(data, path: tuple[str, ...] = (), file_stem: str | None = None):
    """Recursively yield ``(key_name, value)`` for credential-shaped
    leaves anywhere in a parsed JSON/YAML tree."""
    if isinstance(data, dict):
        for k, v in data.items():
            new_path = path + (str(k),)
            if isinstance(v, dict) or isinstance(v, list):
                yield from _walk_creds_in_obj(v, new_path, file_stem)
                continue
            if v is None or v == "":
                continue
            if not isinstance(v, (str, int, float, bool)):
                continue
            value_str = str(v)
            if not value_str.strip():
                continue

            key_str = str(k)
            if _LEAF_CRED_PATTERN.match(key_str):
                synth = _synth_cred_name(new_path, file_stem=file_stem)
                if synth is not None:
                    yield synth, value_str
            elif _is_envvar_style(key_str) and _looks_credential(key_str):
                yield key_str, value_str
    elif isinstance(data, list):
        for i, item in enumerate(data):
            yield from _walk_creds_in_obj(item, path + (str(i),), file_stem)


def _ai_tool_dirs(home: Path, env: dict | None = None) -> list[Path]:
    out = [home / rel for rel in _AI_TOOL_HOME_DIRS]
    if env is not None:
        out.extend(_env_override_dirs(env))
    return out


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
        if lower.endswith((".json", ".yaml", ".yml")):
            pairs = _parse_agent_config(path)
        else:
            # .env, .env.local, credentials, auth — line-based
            pairs = _parse_env_file(path)

        for k, v in pairs:
            yield k, v, display, str(path)


def _walk_for_cred_files(root: Path):
    """Yield every file under ``root`` worth opening for credentials.

    Walk strategy:

    - ``os.walk(..., followlinks=False)`` so symlink loops can't hang
      the scan and we never wander out of the root via a stray link.
    - ``_WALK_SKIP_DIRS`` is pruned in-place from ``dirnames`` so we
      never descend into ``node_modules``, Chromium caches, etc.
    - Hidden subdirectories *below* the root are skipped. The root
      itself is allowed to be hidden — that's how we got into
      ``~/.openclaw`` in the first place.

    Filter strategy: parse anything structured (.json / .yaml / .yml /
    .env*), minus a tiny denylist of dependency-management noise
    (package.json, lockfiles). The recursive cred extractor's leaf-
    key shape is the *real* filter — file names are unreliable across
    agents, every tool ships its config under a different name.
    """
    if not _is_dir_safe(root):
        return
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str, followlinks=False):
        is_root_level = Path(dirpath) == root
        dirnames[:] = [
            d for d in dirnames
            if d not in _WALK_SKIP_DIRS
            and (is_root_level or not d.startswith("."))
        ]
        for fname in filenames:
            if _is_scannable_filename(fname):
                yield Path(dirpath) / fname


def _is_dir_safe(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


_MAX_CONFIG_FILE_SIZE = 5 * 1024 * 1024  # 5 MB; bigger files are state, not config


def _parse_agent_config(path: Path) -> list[tuple[str, str]]:
    """Recursively extract every credential-shaped leaf from a JSON
    or YAML file.

    No top-level container assumptions, no MCP-specific code paths.
    The walker finds creds wherever they live in the tree and the
    name synthesizer produces ``BRAVE_API_KEY`` from
    ``plugins.entries.brave.config.webSearch.apiKey``.
    """
    out: list[tuple[str, str]] = []
    try:
        if path.stat().st_size > _MAX_CONFIG_FILE_SIZE:
            return out
    except OSError:
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            import json as _json
            data = _json.loads(text)
        else:
            data = yaml.safe_load(text)
    except (ValueError, yaml.YAMLError):
        return out

    if not isinstance(data, (dict, list)):
        return out

    file_stem = path.stem
    for name, value in _walk_creds_in_obj(data, file_stem=file_stem):
        out.append((name, value))
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
