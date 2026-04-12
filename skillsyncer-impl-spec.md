# SkillSyncer — Implementation Spec

Read `skillsyncer-spec.md` for the full product design.
This document tells you exactly what to build and how.

## Project Structure

```
skillsyncer/
├── skillsyncer/
│   ├── __init__.py              # version string
│   ├── cli.py                   # click CLI — all commands
│   ├── scanner.py               # regex secret detection
│   ├── renderer.py              # ${{}} template rendering
│   ├── filler.py                # auto-fill from env, identity, cascade
│   ├── guarder.py               # auto-fix secrets in files
│   ├── discoverer.py            # environment scanning for init
│   ├── reporter.py              # JSON report generation/reading
│   ├── identity.py              # identity.yaml read/write
│   ├── config.py                # config.yaml read/write
│   ├── state.py                 # state.yaml read/write
│   ├── hooks.py                 # git hook installation/templates
│   └── patterns.py              # built-in secret detection patterns
├── operator/
│   └── SKILL.md                 # the agent operator skill
├── templates/
│   ├── pre-push.sh              # git pre-push hook template
│   ├── post-merge.sh            # git post-merge hook template
│   └── preamble.md              # <!-- skillsyncer:require --> block
├── install.sh                   # curl-able installer script
├── pyproject.toml               # packaging (pip install skillsyncer)
├── README.md
├── LICENSE                      # MIT
└── tests/
    ├── test_scanner.py
    ├── test_renderer.py
    ├── test_filler.py
    ├── test_guarder.py
    └── fixtures/
        ├── sample_skill/
        │   ├── manifest.yaml
        │   └── SKILL.md
        └── sample_identity.yaml
```

## Dependencies

```toml
# pyproject.toml
[project]
name = "skillsyncer"
version = "0.1.0"
requires-python = ">=3.9"
dependencies = [
    "click>=8.0",
    "pyyaml>=6.0",
    "gitpython>=3.1",
]

[project.scripts]
skillsyncer = "skillsyncer.cli:main"
```

No other dependencies. Keep it minimal.

---

## Module Specifications

### patterns.py — Secret detection patterns

```python
"""Built-in regex patterns for secret detection."""

BLOCK_PATTERNS = [
    {
        "pattern": r'(?:sk-|key-|token-|api[_\-]?key)[a-zA-Z0-9_\-]{8,}',
        "label": "API key",
    },
    {
        "pattern": r'Bearer\s+[A-Za-z0-9\-._~+/]{20,}',
        "label": "Bearer token",
    },
    {
        "pattern": r'https?://[^${}\s]+:[^${}\s]+@',
        "label": "Credentials in URL",
    },
    {
        "pattern": r'AKIA[0-9A-Z]{16}',
        "label": "AWS access key",
    },
    {
        "pattern": r'-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----',
        "label": "Private key",
    },
    {
        "pattern": r'ghp_[A-Za-z0-9]{36}',
        "label": "GitHub personal access token",
    },
    {
        "pattern": r'xox[bpoas]-[A-Za-z0-9\-]{10,}',
        "label": "Slack token",
    },
]

ALLOW_PATTERNS = [
    r'\$\{\{[A-Z_][A-Z0-9_]*\}\}',  # SkillSyncer placeholders
]
```

### scanner.py — Secret detection engine

Core function: given a file's content and an identity secrets dict,
return a list of detections.

```python
def scan_content(
    content: str,
    identity_secrets: dict[str, str],
    extra_block: list[dict] | None = None,
    extra_allow: list[str] | None = None,
) -> list[Detection]:
    """
    Scan content for potential secrets.

    Returns list of Detection objects:
    {
        "line": int,           # 1-indexed line number
        "column": int,         # 0-indexed column
        "matched_text": str,   # the actual matched string (truncated for display)
        "pattern_label": str,  # which pattern matched
        "identity_key": str | None,  # if matched against identity.yaml, the key name
    }

    Algorithm:
    1. For each line in content:
       a. Check if line matches any ALLOW pattern → skip those regions
       b. For each BLOCK pattern, find all matches not in allowed regions
       c. For each identity secret value, find exact matches not in allowed regions
    2. Deduplicate overlapping detections
    3. Return sorted by line number
    """
```

`scan_staged_files(repo_path, identity)` — wrapper that runs
`git diff --cached --name-only`, reads each file, calls `scan_content`.

### renderer.py — Template engine

The entire template engine. Must be dead simple.

```python
PLACEHOLDER_RE = re.compile(r'\$\{\{([A-Z_][A-Z0-9_]*)\}\}')

def render_skill(
    skill_md: str,
    manifest: dict,
    identity: dict,
) -> tuple[str, list[str]]:
    """
    Render a SKILL.md by substituting ${{KEY}} placeholders.

    Resolution order for each ${{KEY}}:
    1. identity["overrides"][skill_name][KEY]
    2. identity["secrets"][KEY]
    3. manifest["values"][KEY]
    4. Unresolved — leave as ${{KEY}}

    Returns:
    - rendered: str — the rendered content
    - unfilled: list[str] — list of KEY names that weren't resolved

    IMPORTANT: Never write rendered output to a git-tracked directory.
    Only write to agent target directories or keep in memory.
    """
```

`render_all_skills(config, identity)` — iterates all skills from
all sources in config, renders each, writes to target agent dirs,
returns a fill report dict.

### filler.py — Auto-fill from available sources

```python
def auto_fill(
    skills: dict,  # {name: manifest_dict}
    identity: dict,
    env: dict | None = None,  # os.environ if None
) -> tuple[dict, dict]:
    """
    Try to resolve unfilled placeholders from available sources.

    For each unfilled ${{KEY}} across all skills:
    1. identity.secrets[KEY] — already have it?
    2. os.environ[KEY] — set in shell?
    3. Shared: another skill already resolved this KEY?
       (cascading fill — the KEY name matches, reuse the value)
    4. manifest.values[KEY] — default value?

    Returns:
    - newly_found: dict[str, str] — {KEY: value} that were resolved
    - still_missing: dict[str, list[dict]] — {skill_name: [{key, description, checked}]}
    """
```

### guarder.py — Auto-fix secrets in files

```python
def guard_fix(
    repo_path: str,
    identity: dict,
    detections: list[Detection],
) -> list[Fix]:
    """
    Auto-replace detected secrets with ${{PLACEHOLDER}} names.

    For each detection:
    1. If detection.identity_key is set:
       → Replace matched_text with ${{identity_key}}
       → This is the common case when identity.yaml is populated

    2. If detection.identity_key is None (first push, unknown secret):
       → Mark as unresolved — agent will name it later
       → Do NOT generate generic names like API_KEY_1

    Returns list of Fix objects:
    {
        "file": str,
        "line": int,
        "original": str,        # truncated for display
        "replacement": str,     # "${{KEY_NAME}}" or None if unresolved
        "status": "fixed" | "unresolved",
        "identity_key": str | None,
    }

    Side effects:
    - Writes modified files in-place
    - Updates manifest.yaml requires.secrets if new placeholder added
    - Adds value to identity.yaml secrets if not already there
    """
```

### discoverer.py — Environment scanning for init

```python
def discover() -> DiscoverResult:
    """
    Scan the user's machine to propose a SkillSyncer setup.

    Returns DiscoverResult:
    {
        "agents": [
            {"name": "claude-code", "path": "~/.claude/skills/", "found": True},
            {"name": "cursor", "path": "~/.cursor/skills/", "found": True},
            {"name": "openclaw", "path": "~/openclaw/skills/", "found": False},
        ],
        "existing_skills": [
            {"name": "energy-diagnose", "agent": "claude-code",
             "has_placeholders": True, "has_hardcoded_secrets": False},
        ],
        "credentials": [
            {"key": "GATEWAY_URL", "value": "https://...",
             "source": ".env.local", "path": "/home/user/.env.local"},
            {"key": "OPENAI_KEY", "value": "sk-...",
             "source": "shell environment", "path": None},
        ],
        "git": {
            "gh_authenticated": True,
            "suggested_repo_name": "agent-skills",
            "current_project_remote": "github.com/edgenesis/shifu",
        }
    }

    Scanning strategy:
    a. Agents: check well-known paths
       ~/.claude/  ~/.cursor/  ~/.gemini/  ~/openclaw/
       Also check: ~/.config/github-copilot/

    b. Existing skills: ls each agent's skills/ dir
       For each .md file, check for ${{}} and scan for secrets

    c. Credentials: search these locations in order
       - .env, .env.local, .env.production in ~ and cwd
       - docker-compose.yml, docker-compose.override.yml
       - ~/.kube/config (extract server URLs)
       - ~/.config/ tool-specific configs
       - os.environ — filter for KEY/TOKEN/SECRET/API/URL/WEBHOOK

    d. Git: check if gh CLI exists and is authenticated
       Run: gh auth status (check exit code)
       If in a git repo: git remote get-url origin
    """
```

**Credential scanning rules:**
- Only look at files the user owns (not system files)
- For `.env` files: parse `KEY=VALUE` lines
- For `docker-compose.yml`: parse `environment:` sections
- For shell env: filter by name patterns, skip common system vars
  (PATH, HOME, SHELL, TERM, etc.)
- NEVER log or print discovered credential VALUES during scanning
- Store values in memory only until written to identity.yaml

### reporter.py — Report generation

```python
def create_report(report_type: str) -> Report:
    """Create a new report (type: 'fill' or 'guard')."""

def update_report(report: Report, attempt: dict):
    """Add an attempt to the report."""

def finalize_report(report: Report, status: str):
    """Set final_status and write to ~/.skillsyncer/reports/."""

def latest_report(report_type: str | None = None) -> Report | None:
    """Read the most recent report, optionally filtered by type."""
```

Reports are JSON files at `~/.skillsyncer/reports/<type>-<timestamp>.json`.
Clean up reports older than 30 days on each `finalize_report` call.

### identity.py — Identity file management

```python
IDENTITY_PATH = Path.home() / ".skillsyncer" / "identity.yaml"

def read_identity() -> dict:
    """Read identity.yaml. Returns {"secrets": {}, "overrides": {}} if empty/missing."""

def write_identity(identity: dict):
    """Write identity.yaml. Creates parent dirs if needed."""

def set_secret(key: str, value: str):
    """Add or update a single secret in identity.yaml."""

def list_secret_keys() -> list[str]:
    """Return secret key names (not values)."""
```

**File format:**
```yaml
secrets:
  GATEWAY_URL: https://gw.site-a.edgenesis.com
  GATEWAY_KEY: sk-xxxxx

overrides:
  energy-diagnose:
    alarm_threshold: 0.95
    response_style: concise
```

### config.py — Config file management

```python
CONFIG_PATH = Path.home() / ".skillsyncer" / "config.yaml"

def read_config() -> dict:
    """Read config.yaml. Returns {"sources": [], "targets": []} if empty/missing."""

def write_config(config: dict):
    """Write config.yaml."""

def add_source(url: str, name: str):
    """Add a git source to config.yaml."""

def detect_targets() -> list[dict]:
    """Auto-detect installed agent skill directories."""
```

**Target detection logic:**
```python
KNOWN_AGENTS = [
    {"name": "claude-code", "paths": ["~/.claude/skills"]},
    {"name": "cursor", "paths": ["~/.cursor/skills"]},
    {"name": "windsurf", "paths": ["~/.windsurf/skills"]},
    {"name": "gemini", "paths": ["~/.gemini/skills"]},
    {"name": "codex", "paths": ["~/.codex/skills"]},
]
```

### state.py — Sync state tracking

```python
STATE_PATH = Path.home() / ".skillsyncer" / "state.yaml"

def read_state() -> dict:
def write_state(state: dict):
def update_skill_state(skill_name: str, **fields):
def get_drift() -> list[dict]:
    """Compare state.yaml against current repo versions."""
```

### hooks.py — Git hook management

```python
def install_hooks(repo_path: str):
    """Install pre-push and post-merge hooks into a git repo.

    Writes hook scripts to .git/hooks/pre-push and .git/hooks/post-merge.
    Makes them executable (chmod +x).
    If hooks already exist, prepend skillsyncer section with a guard comment.
    """

def uninstall_hooks(repo_path: str):
    """Remove skillsyncer sections from git hooks."""
```

**pre-push hook template** (see `templates/pre-push.sh`):

```bash
#!/bin/bash
# [skillsyncer:hook] — do not edit this section
set -euo pipefail

MAX_RETRIES=5
ATTEMPT=0
REPORT_FILE="$HOME/.skillsyncer/reports/guard-$(date +%s).json"
skillsyncer report create --type=guard --path="$REPORT_FILE"

while [ $ATTEMPT -lt $MAX_RETRIES ]; do
  ATTEMPT=$((ATTEMPT + 1))

  ISSUES=$(skillsyncer scan --staged --format=json 2>&1)
  EXIT=$?

  if [ $EXIT -eq 0 ]; then
    skillsyncer report finalize "$REPORT_FILE" --status=passed
    echo "[SkillSyncer] ✓ Push clean (attempt $ATTEMPT)" >&2
    break
  fi

  skillsyncer report update "$REPORT_FILE" --attempt=$ATTEMPT --issues="$ISSUES"
  echo "[SkillSyncer] Attempt $ATTEMPT/$MAX_RETRIES — auto-fixing..." >&2
  skillsyncer guard --fix --report="$REPORT_FILE" 2>&1
  git add -u
done

if [ $ATTEMPT -ge $MAX_RETRIES ]; then
  FINAL_CHECK=$(skillsyncer scan --staged --format=json 2>&1)
  if [ $? -ne 0 ]; then
    skillsyncer report finalize "$REPORT_FILE" --status=failed
    echo "" >&2
    echo "══════════════════════════════════════════════════════" >&2
    echo " SkillSyncer: push FAILED after $MAX_RETRIES attempts" >&2
    echo " Report: $REPORT_FILE" >&2
    echo "══════════════════════════════════════════════════════" >&2
    exit 1
  fi
fi
# [/skillsyncer:hook]
```

**post-merge hook template** (see `templates/post-merge.sh`):

```bash
#!/bin/bash
# [skillsyncer:hook] — do not edit this section
CHANGED=$(skillsyncer diff-since-last-sync 2>&1)
if [ -n "$CHANGED" ]; then
  REPORT_FILE="$HOME/.skillsyncer/reports/fill-$(date +%s).json"
  skillsyncer fill --auto --report="$REPORT_FILE" 2>&1
  skillsyncer render --report="$REPORT_FILE" 2>&1
  STATUS=$(skillsyncer report status "$REPORT_FILE" 2>&1)
  if [ "$STATUS" = "partial" ]; then
    echo "[SkillSyncer] Some skills need credentials. Your agent will help." >&2
  else
    echo "[SkillSyncer] ✓ All skills rendered." >&2
  fi
fi
# [/skillsyncer:hook]
```

---

## CLI Commands (cli.py)

Use `click` for all commands. Group under `skillsyncer`.

```python
@click.group()
def main():
    """SkillSyncer — agent skills that sync, fill, and protect themselves."""

@main.command()
def init():
    """One-time setup: scan environment, propose setup, create config."""
    # 1. Create ~/.skillsyncer/ if not exists
    # 2. Run discoverer.discover()
    # 3. Print proposal to stdout as formatted text
    # 4. Prompt user: "Sound good? [Y/n]" and repo choice [A/B/C]
    # 5. Write identity.yaml with discovered credentials
    # 6. Write config.yaml with detected targets
    # 7. If repo chosen: create/clone repo, install hooks
    # 8. Install operator SKILL.md into all detected agent dirs
    # 9. Run render for all discovered skills
    # 10. Print summary

@main.command()
@click.argument("url")
@click.option("--name", default=None, help="Alias for this source")
def add(url, name):
    """Add a skill source repo and install git hooks."""
    # 1. Clone repo into ~/.skillsyncer/repos/<name>/
    # 2. Install hooks
    # 3. Add to config.yaml sources
    # 4. Scan manifests, report required secrets
    # 5. Run fill --auto + render

@main.command()
@click.option("--report", default=None, help="Report file path")
def render(report):
    """Hydrate ${{}} placeholders and write to agent target dirs."""
    # 1. Read config, identity
    # 2. For each skill in each source:
    #    a. Read manifest.yaml + SKILL.md
    #    b. Render with renderer.render_skill()
    #    c. Write to each target agent dir
    # 3. Update state.yaml
    # 4. If report path given, update report with results
    # Exit 0 if all filled, exit 1 if any unfilled

@main.command(name="fill")
@click.option("--auto", "auto_fill_flag", is_flag=True)
@click.option("--report", default=None)
def fill_cmd(auto_fill_flag, report):
    """Resolve unfilled placeholders from env, identity, cascade."""
    # 1. Collect all unfilled placeholders across all skills
    # 2. Run filler.auto_fill()
    # 3. Write newly found secrets to identity.yaml
    # 4. Update report
    # Exit 0 if found new values, exit 1 if nothing new

@main.command()
@click.option("--staged", is_flag=True, help="Only scan staged files")
@click.option("--format", "fmt", default="human", type=click.Choice(["human", "json"]))
def scan(staged, fmt):
    """Detect potential secrets in files (regex, no AI)."""
    # 1. Read identity secrets for cross-reference
    # 2. Read extra patterns from config.yaml
    # 3. If --staged: scan staged files only
    #    Else: scan all files in current directory
    # 4. Output detections in chosen format
    # Exit 0 if clean, exit 1 if issues found

@main.command()
@click.option("--fix", is_flag=True, help="Auto-replace detected secrets")
@click.option("--report", default=None)
def guard(fix, report):
    """Scan and optionally auto-fix secrets in skill files."""
    # 1. Run scan on staged files
    # 2. If --fix: run guarder.guard_fix() to replace values
    # 3. Update report with fixes
    # 4. Print summary of what was fixed / unresolved

@main.command(name="diff-since-last-sync")
def diff_since_last_sync():
    """Show skills that changed since last render."""
    # Compare current repo state against state.yaml hashes
    # Print changed skill names

@main.command()
@click.argument("key")
@click.argument("value")
def secret_set(key, value):
    """Add or update a secret in identity.yaml."""
    identity.set_secret(key, value)
    click.echo(f"Set {key}")

@main.command()
def secret_list():
    """Show secret key names (not values)."""
    for key in identity.list_secret_keys():
        click.echo(f"  {key}")

@main.command()
def status():
    """Show skills, versions, missing secrets."""
    # Read state.yaml, identity.yaml, config.yaml
    # For each skill: version, source, status (synced/partial/drifted)
    # List unfilled secrets with manifest descriptions
    # Print formatted table

@main.group()
def report():
    """Manage guard and fill reports."""

@report.command()
@click.option("--type", "rtype", default=None, type=click.Choice(["fill", "guard"]))
def latest(rtype):
    """Print the most recent report."""

@report.command(name="list")
def report_list():
    """List all reports."""

@report.command()
@click.option("--days", default=30, help="Delete reports older than N days")
def clean(days):
    """Prune old reports."""
```

---

## Operator SKILL.md

Place at `operator/SKILL.md`. This is the full text — write it
exactly as specified in the design spec sections:
- "DISCOVER" flow (from the init/onboarding section)
- "FILL" flow (Phase 2 agent-driven fill)
- "GUARD-ASSIST" flow (Phase 2 agent-driven push resolution)
- "SHARE" flow (first-time sharing)
- "REPORT" flow (reading and presenting reports)
- "STATUS" flow
- "ONBOARD" flow (runs during init)
- Rules (what agent CAN/CANNOT do)

Copy all of these from the design spec into a single SKILL.md.
Add the preamble at the top:

```markdown
<!-- skillsyncer:operator -->
# SkillSyncer Operator

You manage the user's AI agent skills using SkillSyncer.
You have two roles:
- Primary filler on pull (find and fill credentials)
- Helper on push (name placeholders, resolve guard failures)

The CLI handles all security decisions. You handle UX.

[... rest of operator skill content from design spec ...]
```

---

## install.sh

```bash
#!/bin/bash
set -euo pipefail

VERSION="0.1.0"
INSTALL_DIR="$HOME/.local/bin"

echo "Installing SkillSyncer v$VERSION..."

# Ensure install directory exists and is in PATH
mkdir -p "$INSTALL_DIR"
if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
  echo "export PATH=\"$INSTALL_DIR:\$PATH\"" >> "$HOME/.bashrc"
  echo "export PATH=\"$INSTALL_DIR:\$PATH\"" >> "$HOME/.zshrc" 2>/dev/null || true
  export PATH="$INSTALL_DIR:$PATH"
fi

# Install via pip
pip install --user skillsyncer 2>/dev/null || pip install --user --break-system-packages skillsyncer

echo ""
echo "✓ SkillSyncer installed"
echo ""
echo "Run:  skillsyncer init"
echo ""
```

---

## Key Algorithms

### Placeholder rendering

```
Input: "Connect to ${{GATEWAY_URL}} with ${{GATEWAY_KEY}}"
Identity: {secrets: {GATEWAY_URL: "https://...", GATEWAY_KEY: "sk-..."}}
Output: "Connect to https://... with sk-..."
Unfilled: []
```

Use `re.sub` with a callback that looks up each match:
```python
def _replace(match, values):
    key = match.group(1)
    if key in values:
        return values[key]
    return match.group(0)  # leave as-is
```

### Secret cross-reference

When scanning, after checking all regex patterns, also check
every value in `identity.secrets` as a literal substring match
against the file content. This catches secrets that don't match
any regex pattern but ARE known to the user.

```python
for key, value in identity_secrets.items():
    if len(value) >= 8:  # skip short values to avoid false positives
        for i, line in enumerate(lines):
            if value in line:
                # Check it's not inside a ${{}} placeholder
                detections.append(Detection(
                    line=i+1,
                    matched_text=value[:12] + "...",
                    pattern_label=f"Known secret from identity.yaml",
                    identity_key=key,
                ))
```

### Cascading fill

```python
def auto_fill(skills, identity, env):
    newly_found = {}

    for skill_name, manifest in skills.items():
        for secret in manifest.get("requires", {}).get("secrets", []):
            key = secret["name"] if isinstance(secret, dict) else secret
            if key in identity.get("secrets", {}):
                continue  # already have it

            # Check env
            if key in (env or os.environ):
                newly_found[key] = (env or os.environ)[key]
                continue

            # Check if another skill already resolved this key
            if key in newly_found:
                continue  # will be written after loop

            # Check manifest defaults (non-secret values)
            if key in manifest.get("values", {}):
                newly_found[key] = str(manifest["values"][key])

    return newly_found
```

Run this in a loop (max 3 passes) because resolving one key
might make `newly_found` available for the cascade check on
the next pass.

---

## Testing

### test_scanner.py
- Test each regex pattern with positive and negative examples
- Test identity cross-reference matching
- Test that ${{PLACEHOLDER}} regions are excluded from detection
- Test with real-world-like SKILL.md files

### test_renderer.py
- Test basic ${{}} substitution
- Test resolution order: overrides > secrets > values
- Test unfilled placeholders are left as-is
- Test with no identity (empty dict)

### test_filler.py
- Test env var resolution
- Test cascading fill across multiple skills
- Test that already-filled keys are skipped

### test_guarder.py
- Test replacement with known identity key
- Test unresolved detection (no identity match)
- Test manifest.yaml update when new placeholder added
- Test that allowed patterns are not replaced

---

## Build Order

### Phase 1 — Core engine (start here)

1. `patterns.py` — define all regex patterns
2. `scanner.py` — implement `scan_content` and `scan_staged_files`
3. `renderer.py` — implement `render_skill` and `render_all_skills`
4. `identity.py` — read/write identity.yaml
5. `config.py` — read/write config.yaml
6. Write tests for scanner and renderer

### Phase 2 — Fill and guard

7. `filler.py` — implement `auto_fill` with cascade logic
8. `guarder.py` — implement `guard_fix`
9. `reporter.py` — JSON report create/update/finalize/read
10. `state.py` — state tracking
11. Write tests for filler and guarder

### Phase 3 — CLI and hooks

12. `cli.py` — all commands (init, add, render, fill, scan, guard,
    secret set, secret list, status, report)
13. `hooks.py` — hook installation with templates
14. `templates/pre-push.sh` and `templates/post-merge.sh`

### Phase 4 — Discovery and operator

15. `discoverer.py` — environment scanning
16. `operator/SKILL.md` — the agent skill (copy from design spec)
17. `templates/preamble.md`
18. `install.sh`

### Phase 5 — Packaging

19. `pyproject.toml` — packaging config
20. `README.md` — user-facing docs (extract from design spec)
21. End-to-end integration tests

---

## Critical Implementation Rules

1. **Never write rendered secrets to git-tracked directories.**
   Rendered SKILL.md files go to agent target dirs (e.g.,
   `~/.claude/skills/`) which are NOT inside a skillsyncer repo.

2. **Never print secret values to stdout/stderr.**
   Print key names only. Use truncation (`sk-abc...23`) in reports.

3. **identity.yaml must never be in a git repo.**
   During `init` and `add`, add `identity.yaml` to the repo's
   `.gitignore` if not already there.

4. **Hooks must be idempotent.**
   Running `install_hooks` twice must not duplicate hook content.
   Use the `[skillsyncer:hook]` / `[/skillsyncer:hook]` markers
   to find and replace existing sections.

5. **All file operations use atomic writes.**
   Write to a temp file first, then `os.rename` to the target.
   This prevents corruption if the process is killed mid-write.

6. **The scanner never has false negatives for identity values.**
   If a value exists in `identity.secrets` and appears in a file,
   it MUST be detected. Regex patterns can have false negatives
   (unknown key formats), but identity cross-reference cannot.

7. **Exit codes matter.** The hooks depend on them.
   - `scan`: 0 = clean, 1 = issues found
   - `fill --auto`: 0 = found new values, 1 = nothing new
   - `render`: 0 = all filled, 1 = some unfilled
   - `guard --fix`: 0 = all fixed, 1 = some unresolved
