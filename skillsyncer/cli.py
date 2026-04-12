"""SkillSyncer command-line interface.

A thin layer over the modules: every command reads/writes
``identity.yaml``, ``config.yaml``, and ``state.yaml`` and shells the
work out. Exit codes are load-bearing — git hooks depend on them.

Built on stdlib ``argparse`` to keep the dependency footprint at a
single third-party package (PyYAML).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import yaml

from . import __version__, hooks, paths
from ._io import atomic_write
from .config import detect_targets, read_config, write_config
from .discoverer import _discover_agents, discover
from .filler import auto_fill
from .guarder import guard_fix
from .identity import list_secret_keys, read_identity, set_secret, write_identity
from .renderer import render_all_skills
from .reporter import (
    clean_old_reports,
    create_report,
    finalize_report,
    latest_report,
    list_reports,
    read_report,
    update_report,
)
from .scanner import scan_file, scan_staged_files
from .state import get_drift, hash_file, read_state, write_state

_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
}
_TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".yaml", ".yml", ".json", ".toml", ".ini",
    ".env", ".sh", ".py", ".js", ".ts", ".rb", ".go", ".rs", ".java",
    ".kt", ".swift", ".php", ".c", ".cpp", ".h", ".hpp", ".html", ".css",
}


# ---------------------------------------------------------------------------
# Color + visual helpers
# ---------------------------------------------------------------------------


class C:
    """ANSI color helpers. Outputs escape codes only when stdout is a
    TTY and the user hasn't set ``NO_COLOR``. ``FORCE_COLOR`` overrides.
    Tests use capsys, which is non-tty, so colors auto-disable."""

    enabled = False

    @classmethod
    def init(cls) -> None:
        if os.environ.get("NO_COLOR"):
            cls.enabled = False
        elif os.environ.get("FORCE_COLOR"):
            cls.enabled = True
        else:
            cls.enabled = sys.stdout.isatty()

    @classmethod
    def _wrap(cls, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if cls.enabled else text

    @classmethod
    def bold(cls, t: str) -> str: return cls._wrap("1", t)
    @classmethod
    def dim(cls, t: str) -> str: return cls._wrap("2", t)
    @classmethod
    def red(cls, t: str) -> str: return cls._wrap("31", t)
    @classmethod
    def green(cls, t: str) -> str: return cls._wrap("32", t)
    @classmethod
    def yellow(cls, t: str) -> str: return cls._wrap("33", t)
    @classmethod
    def blue(cls, t: str) -> str: return cls._wrap("34", t)
    @classmethod
    def magenta(cls, t: str) -> str: return cls._wrap("35", t)
    @classmethod
    def cyan(cls, t: str) -> str: return cls._wrap("36", t)


def _out(msg: str = "") -> None:
    print(msg)


def _err(msg: str = "") -> None:
    print(msg, file=sys.stderr)


# Box-drawing + status glyphs as named constants. Pulled out so we
# can interpolate them inside f-strings without tripping Python 3.11's
# "no backslashes inside f-string expressions" rule.
GLYPH_CHECK = "\u2713"      # ✓
GLYPH_CROSS = "\u2717"      # ✗
GLYPH_BULLET = "\u00b7"     # ·
GLYPH_ARROW = "\u2192"      # →
GLYPH_ELLIP = "\u2026"      # …
BOX_HEAVY = "\u2500" * 60   # ─ × 60
BOX_TL = "\u256d"           # ╭
BOX_TR = "\u256e"           # ╮
BOX_BL = "\u2570"           # ╰
BOX_BR = "\u256f"           # ╯
BOX_V = "\u2502"            # │


def _print_banner() -> None:
    """Compact banner shown at the top of ``init``."""
    top = "  " + BOX_TL + ("\u2500" * 60) + BOX_TR
    bot = "  " + BOX_BL + ("\u2500" * 60) + BOX_BR
    mid = "  " + BOX_V + "  " + "SkillSyncer" + "  agent skills that sync, fill, protect themselves" + "  " + BOX_V
    _out("")
    _out(C.cyan(top))
    _out(C.cyan("  " + BOX_V + "  ") + C.bold("SkillSyncer") + C.dim("  agent skills that sync, fill, protect themselves") + C.cyan("  " + BOX_V))
    _out(C.cyan(bot))
    _out("")


def _print_next_steps(steps: list[tuple[str, str | None, str]]) -> None:
    """Print a numbered "Next steps" block.

    Each step is ``(title, command_or_None, explanation)``. The
    explanation may be multi-line; lines are dimmed.
    """
    _out("")
    _out(C.bold(C.yellow("\u250c\u2500 What's next? \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")))
    for i, (title, command, explanation) in enumerate(steps, 1):
        _out("")
        _out(f"  {C.bold(C.yellow(f'{i}.'))} {C.bold(title)}")
        if command:
            _out(f"       {C.cyan(command)}")
        if explanation:
            for line in explanation.strip().split("\n"):
                _out(f"       {C.dim(line)}")
    _out("")


def _section(title: str) -> str:
    return C.bold(C.cyan(title))


def _ok(text: str) -> str:
    return C.green(GLYPH_CHECK) + " " + text


def _warn(text: str) -> str:
    return C.yellow("!") + " " + text


def _err_marker(text: str) -> str:
    return C.red(GLYPH_CROSS) + " " + text


# ---------------------------------------------------------------------------
# init / add
# ---------------------------------------------------------------------------


def _consent_prompt(plan: list[dict]) -> bool:
    """Show the user the credential-scan plan and ask for consent.

    Returns True for yes (default), False for no. On a non-interactive
    stdin, defaults to False so a curl-piped invocation never silently
    reads files the user didn't approve.
    """
    existing = [p for p in plan if p["exists"]]
    skipped = len(plan) - len(existing)

    _out("")
    _out("\u250c\u2500 Credential scan consent " + "\u2500" * 38)
    _out("\u2502")
    _out("\u2502  SkillSyncer would like to read these locations to find")
    _out("\u2502  credentials it can pre-fill in your skills:")
    _out("\u2502")

    by_kind: dict[str, list[dict]] = {}
    for entry in existing:
        by_kind.setdefault(entry["kind"], []).append(entry)

    LABELS = {
        "project": "Project (./)",
        "home": "User home (~)",
        "ai-tool": "AI tool config dirs",
        "shell": "Shell environment",
    }
    for kind in ("project", "home", "shell", "ai-tool"):
        if kind not in by_kind:
            continue
        _out(f"\u2502  {LABELS[kind]}:")
        for e in by_kind[kind]:
            _out(f"\u2502    \u2713 {e['display']}")
        _out("\u2502")

    if skipped:
        _out(f"\u2502  ({skipped} additional locations checked but not present)")
        _out("\u2502")
    _out("\u2502  All values stay on this machine. The CLI never prints")
    _out("\u2502  credential VALUES \u2014 only key NAMES.")
    _out("\u2514" + "\u2500" * 62)
    _out("")

    if not sys.stdin.isatty():
        _err("[skillsyncer] non-interactive shell \u2014 skipping credential scan.")
        _err("[skillsyncer] re-run `skillsyncer init --yes` to scan, or")
        _err("[skillsyncer] `skillsyncer init --no-scan` to silence this notice.")
        return False

    try:
        answer = input("Scan these locations now? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        _out("")
        return False
    return answer in ("", "y", "yes")


def cmd_init(args: argparse.Namespace) -> int:
    home = paths.home()
    home.mkdir(parents=True, exist_ok=True)

    # Decide whether we have permission to scan credentials.
    if args.no_scan:
        scan_creds = False
    elif args.as_json:
        # JSON mode is for the operator agent — never scans without
        # an explicit --scan-credentials flag, since the agent is
        # responsible for asking the user for consent first.
        scan_creds = bool(args.scan_credentials)
    elif args.yes:
        scan_creds = True
    else:
        # Interactive: do a no-cred discover first so we can show the
        # plan, then ask the user.
        preview = discover(scan_credentials=False)
        scan_creds = _consent_prompt(preview["credential_scan_plan"])

    proposal = discover(scan_credentials=scan_creds)

    if args.as_json:
        safe = dict(proposal)
        safe["credentials"] = [
            {"key": c["key"], "source": c["source"], "path": c["path"]}
            for c in proposal["credentials"]
        ]
        _out(json.dumps(safe, indent=2))
        return 0

    # Interactive (human) output starts here.
    _print_banner()

    config = read_config()
    config.setdefault("sources", [])
    if not config.get("targets"):
        config["targets"] = [
            {"name": a["name"], "path": a["path"], "found": a["found"]}
            for a in proposal["agents"] if a["found"]
        ] or detect_targets()
    write_config(config)

    if not paths.identity_path().exists():
        write_identity({"secrets": {}, "overrides": {}})

    _out(_ok(C.bold("SkillSyncer initialized")) + "  " + C.dim(str(home)))
    _out("")

    found_agents = [a for a in proposal["agents"] if a["found"]]
    if found_agents:
        _out(_section("Agents detected") + C.dim(f"  ({len(found_agents)})"))
        for a in found_agents:
            _out(f"  {C.green(GLYPH_CHECK)} {C.bold(a['name']):<22} {C.dim(a['path'])}")
    else:
        _out(_section("Agents detected") + C.dim("  (0)"))
        _out(C.dim("  No known agents on this machine."))

    if proposal["existing_skills"]:
        SKILLS_PER_AGENT = 8
        by_agent: dict[str, list[dict]] = {}
        for s in proposal["existing_skills"]:
            by_agent.setdefault(s["agent"], []).append(s)

        total = len(proposal["existing_skills"])
        _out("")
        _out(_section("Existing skills") + C.dim(f"  ({total})"))
        for agent_name in sorted(by_agent):
            group = by_agent[agent_name]
            _out(f"  {C.bold(agent_name)} {C.dim(f'({len(group)})')}")
            for s in group[:SKILLS_PER_AGENT]:
                tags = []
                if s["has_placeholders"]:
                    tags.append(C.cyan("placeholders"))
                if s["has_hardcoded_secrets"]:
                    tags.append(C.red("hardcoded-secret"))
                tail = f"  [{', '.join(tags)}]" if tags else ""
                _out(f"    \u00b7 {s['name']}{tail}")
            if len(group) > SKILLS_PER_AGENT:
                _out(C.dim(f"    \u00b7 \u2026 and {len(group) - SKILLS_PER_AGENT} more"))

    _out("")
    if not proposal.get("credential_scan_performed"):
        _out(_section("Credentials") + C.dim("  (scan skipped)"))
        _out(C.dim("  Re-run `skillsyncer init --yes` to scan, or add secrets by hand"))
        _out(C.dim("  with `skillsyncer secret-set <KEY> <VALUE>`."))
    elif proposal["credentials"]:
        by_key: dict[str, list[dict]] = {}
        for c in proposal["credentials"]:
            by_key.setdefault(c["key"], []).append(c)

        unique = len(by_key)
        total = len(proposal["credentials"])
        header = _section("Credentials found") + C.dim(f"  ({unique} unique")
        if total != unique:
            header += C.dim(f", {total} candidates")
        header += C.dim(")")
        _out(header)
        for key in sorted(by_key):
            candidates = by_key[key]
            sources = sorted({c["source"] for c in candidates})
            primary_source = sources[0]
            extras = ""
            if len(candidates) > 1:
                detail = f"{len(candidates)} values"
                if len(sources) > 1:
                    detail += f" across {len(sources)} files"
                extras = "  " + C.yellow(f"({detail})")
            _out(f"  {C.green(GLYPH_CHECK)}  {C.bold(key):<32} {C.dim(primary_source)}{extras}")
    else:
        _out(_section("Credentials found") + C.dim("  (none)"))

    git = proposal["git"]
    if git.get("current_project_remote") or git.get("gh_authenticated"):
        _out("")
        _out(_section("Git"))
        if git.get("current_project_remote"):
            _out(f"  {C.dim('current project remote:')} {git['current_project_remote']}")
        if git.get("gh_authenticated"):
            _out(f"  {C.green(GLYPH_CHECK)} {C.dim('gh CLI authenticated — `skillsyncer add` can clone private repos.')}")

    _print_next_steps([
        (
            "Register a skills repo to sync skills across machines",
            "skillsyncer add git@github.com:you/agent-skills.git",
            "Clones the repo into ~/.skillsyncer/repos/, installs the\n"
            "pre-push and post-merge git hooks, and tracks it as a source.",
        ),
        (
            "List every skill SkillSyncer can see",
            "skillsyncer skills",
            "Shows all SKILL.md files found in your detected agent dirs,\n"
            "grouped by agent, with placeholder / hardcoded-secret flags.",
        ),
        (
            "Hydrate ${{...}} placeholders into your agent dirs",
            "skillsyncer render",
            "Reads any registered source repo and writes rendered skills\n"
            "into ~/.claude/skills/, ~/.cursor/skills/, etc.",
        ),
    ])
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    url = args.url
    name = args.name
    if not name:
        leaf = url.rstrip("/").split("/")[-1]
        if leaf.endswith(".git"):
            leaf = leaf[:-4]
        name = leaf or "source"

    target = paths.repos_dir() / name
    target.parent.mkdir(parents=True, exist_ok=True)

    if args.no_clone:
        if not Path(url).is_dir():
            _err(f"--no-clone requires an existing dir: {url}")
            return 2
        local_path = str(Path(url).resolve())
    else:
        if target.exists():
            _out(f"[skillsyncer] {name} already cloned, pulling\u2026")
            subprocess.run(
                ["git", "-C", str(target), "pull", "--ff-only"],
                check=False,
            )
        else:
            _out(f"[skillsyncer] cloning {url} \u2192 {target}")
            try:
                subprocess.run(["git", "clone", url, str(target)], check=True)
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                _err(f"[skillsyncer] git clone failed: {exc}")
                return 2
        local_path = str(target)
        try:
            hooks.install_hooks(local_path)
            _out("[skillsyncer] hooks installed")
        except FileNotFoundError as exc:
            _err(f"[skillsyncer] hook install skipped: {exc}")

    config = read_config()
    sources = config.setdefault("sources", [])
    for src in sources:
        if src.get("name") == name:
            src.update({"name": name, "url": url, "path": local_path})
            break
    else:
        sources.append({"name": name, "url": url, "path": local_path})
    write_config(config)
    _out("")
    _out(_ok(f"Added source {C.bold(name)}") + "  " + C.dim(local_path))

    _print_next_steps([
        (
            f"Cherry-pick which local skills to publish into {name}",
            "skillsyncer publish",
            "Lists every skill SkillSyncer found in your agent dirs,\n"
            "lets you pick which to share, copies them into the source\n"
            "repo, injects the SkillSyncer preamble (so anyone who\n"
            "pulls the repo gets SkillSyncer auto-installed by their\n"
            "agent), and creates a git commit ready to push.",
        ),
        (
            "Or publish every detected skill at once (not recommended)",
            "skillsyncer publish --all",
            "Same as above but skips the picker. Use this only when you\n"
            "really do want to share every skill currently on this machine.",
        ),
        (
            "Or skip publishing and just render placeholders",
            "skillsyncer render",
            "If the source repo already has skills with ${{...}} placeholders,\n"
            "this hydrates them and writes the rendered files into your\n"
            "agent dirs (~/.claude/skills/, ~/.cursor/skills/, etc.).",
        ),
    ])
    return 0


# ---------------------------------------------------------------------------
# publish — copy local skills into a registered source repo
# ---------------------------------------------------------------------------


def _find_local_skills() -> list[dict]:
    """Return ``[{name, agent, dir, md}]`` for every depth-1 skill
    found under each detected agent dir."""
    home_path = Path.home()
    out: list[dict] = []
    seen: set[str] = set()
    for agent in _discover_agents(home_path):
        agent_dir = Path(agent["path"])
        if not agent_dir.is_dir():
            continue
        try:
            children = sorted(agent_dir.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            md = child / "SKILL.md"
            if not md.is_file():
                continue
            key = str(md.resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "name": child.name,
                "agent": agent["name"],
                "dir": child,
                "md": md,
            })
    return out


def _resolve_publish_target(config: dict, name: str | None):
    sources = config.get("sources") or []
    if not sources:
        return None, "No sources registered. Run `skillsyncer add <git-url>` first."
    if name:
        match = next((s for s in sources if s.get("name") == name), None)
        if match is None:
            return None, f"Source '{name}' not found. Available: {', '.join(s.get('name','?') for s in sources)}"
        return match, None
    if len(sources) == 1:
        return sources[0], None
    names = ", ".join(s.get("name", "?") for s in sources)
    return None, f"Multiple sources registered. Use --source <name>: {names}"


def _interactive_skill_picker(skills: list[dict]) -> list[dict] | None:
    """Print a numbered list grouped by agent and parse a selection
    string like ``"1,3,5-8"`` or ``"all"``. Returns None on cancel."""
    by_agent: dict[str, list[dict]] = {}
    for s in skills:
        by_agent.setdefault(s["agent"], []).append(s)

    flat: list[dict] = []
    _out("\nAvailable skills to publish:")
    for agent_name in sorted(by_agent):
        _out(f"  {agent_name}:")
        for s in by_agent[agent_name]:
            flat.append(s)
            _out(f"    [{len(flat):2d}] {s['name']}")
    _out("")

    if not sys.stdin.isatty():
        _err("[skillsyncer] non-interactive shell \u2014 use --all or --skill NAME")
        return None

    try:
        answer = input(
            'Which to publish? (e.g. "1,3,5-8" or "all"; empty to cancel): '
        ).strip()
    except (EOFError, KeyboardInterrupt):
        _out("")
        return None

    if not answer:
        return None
    if answer.lower() == "all":
        return flat

    indexes: set[int] = set()
    for chunk in answer.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            try:
                a, b = chunk.split("-", 1)
                for i in range(int(a), int(b) + 1):
                    indexes.add(i)
            except ValueError:
                _err(f"invalid range: {chunk}")
                return None
        else:
            try:
                indexes.add(int(chunk))
            except ValueError:
                _err(f"invalid index: {chunk}")
                return None

    return [flat[i - 1] for i in sorted(indexes) if 1 <= i <= len(flat)]


def _copy_skill_tree(src: Path, dst: Path) -> None:
    """Copy every file under ``src`` into ``dst``, creating
    parent dirs as needed. Overwrites individual files but leaves
    other files in ``dst`` untouched."""
    import shutil

    dst.mkdir(parents=True, exist_ok=True)
    for src_file in src.rglob("*"):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(src)
        dst_file = dst / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)


def _inject_preamble_if_missing(skill_md: Path) -> None:
    """Prepend the SkillSyncer preamble to a SKILL.md if it doesn't
    already declare itself as SkillSyncer-managed."""
    if not skill_md.is_file():
        return
    content = skill_md.read_text(encoding="utf-8")
    if "skillsyncer:require" in content:
        return
    preamble_path = Path(__file__).resolve().parent / "templates" / "preamble.md"
    if not preamble_path.is_file():
        return
    preamble = preamble_path.read_text(encoding="utf-8")
    atomic_write(skill_md, preamble + "\n" + content)


def cmd_publish(args: argparse.Namespace) -> int:
    config = read_config()
    target, err = _resolve_publish_target(config, args.source)
    if err:
        _err(f"[skillsyncer] {err}")
        return 2

    target_path = Path(target.get("path") or "").expanduser()
    if not target_path.is_dir():
        _err(f"[skillsyncer] source dir doesn't exist: {target_path}")
        return 2
    if not (target_path / ".git").exists():
        _err(f"[skillsyncer] source isn't a git repo: {target_path}")
        return 2

    available = _find_local_skills()
    if not available:
        _out("[skillsyncer] no skills found in any agent dir.")
        return 0

    if args.all:
        selected = available
    elif args.skill:
        names_arg = set(args.skill)
        selected = [s for s in available if s["name"] in names_arg]
        missing = names_arg - {s["name"] for s in available}
        if missing:
            _err(f"[skillsyncer] skills not found: {', '.join(sorted(missing))}")
            return 2
    else:
        picked = _interactive_skill_picker(available)
        if picked is None:
            _out("[skillsyncer] cancelled.")
            return 0
        selected = picked

    if not selected:
        _out("[skillsyncer] nothing selected.")
        return 0

    _out(f"\n[skillsyncer] copying {len(selected)} skill(s) into {target['name']}\u2026")
    copied: list[dict] = []
    for skill in selected:
        dst_dir = target_path / skill["name"]
        _copy_skill_tree(skill["dir"], dst_dir)
        _inject_preamble_if_missing(dst_dir / "SKILL.md")
        copied.append(skill)
        _out(f"  \u2713 {skill['name']:<28} (from {skill['agent']})")

    # Pre-flight scan: warn if anything we just copied contains a
    # secret. The pre-push hook will catch it on push too — this is
    # just early feedback so the user can clean up before commit.
    identity = read_identity()
    secrets = identity.get("secrets") or {}
    detections: list[dict] = []
    for skill in copied:
        dst_dir = target_path / skill["name"]
        for f in dst_dir.rglob("*"):
            if f.is_file():
                detections.extend(scan_file(f, secrets))

    if detections:
        _err(f"\n[skillsyncer] pre-flight scan found {len(detections)} potential secret(s):")
        for d in detections:
            _err(f"  {d.get('file', '?')}:{d['line']}: {d['pattern_label']}")
        _err("")
        _err("[skillsyncer] Files were copied but NOT committed.")
        _err(f"[skillsyncer] Fix these in your agent dirs first, then re-run publish.")
        _err(f"[skillsyncer] Or run: cd {target_path} && skillsyncer guard --fix")
        return 1

    # Stage + commit (do not push — that's on the user, and the
    # pre-push hook will run a final security scan).
    msg_lines = [
        f"Publish {len(copied)} skill(s) via SkillSyncer",
        "",
    ]
    for s in copied:
        msg_lines.append(f"- {s['name']}")
    msg = "\n".join(msg_lines)

    try:
        subprocess.run(["git", "-C", str(target_path), "add", "."], check=True)
        diff = subprocess.run(
            ["git", "-C", str(target_path), "diff", "--cached", "--quiet"],
            check=False,
        )
        if diff.returncode == 0:
            _out("\n[skillsyncer] no changes to commit (skills already match the source).")
            return 0
        subprocess.run(
            ["git", "-C", str(target_path), "commit", "-m", msg],
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        _err(f"[skillsyncer] git error: {exc}")
        return 2

    _out("")
    _out(_ok(f"{len(copied)} skill(s) committed to {C.bold(target['name'])}"))
    for s in copied:
        _out(f"    {C.dim(GLYPH_BULLET)} {s['name']}")

    _print_next_steps([
        (
            "Send the commit upstream",
            f"git -C {target_path} push",
            "The pre-push hook will run a final regex scan over the staged\n"
            "files before the push goes out. If it finds any hardcoded\n"
            "secret it'll auto-template what it can and ask you to name\n"
            "what it can't.",
        ),
        (
            "Anyone who pulls this repo gets SkillSyncer auto-installed",
            None,
            "Each SKILL.md you just published has the bootstrap preamble\n"
            "injected. When a teammate's agent loads the skill, it sees the\n"
            "preamble, runs the install one-liner itself, scans for\n"
            "credentials, and renders. They never type the install command.",
        ),
    ])
    return 0


# ---------------------------------------------------------------------------
# skills — list every local skill SkillSyncer can see
# ---------------------------------------------------------------------------


def cmd_skills(args: argparse.Namespace) -> int:
    skills = _find_local_skills()

    if args.json:
        payload = [
            {
                "name": s["name"],
                "agent": s["agent"],
                "path": str(s["md"]),
            }
            for s in skills
        ]
        _out(json.dumps(payload, indent=2))
        return 0

    if not skills:
        _out(C.dim("No skills found in any detected agent dir."))
        _out(C.dim("Run `skillsyncer init` first if you haven't yet."))
        return 0

    # Optionally filter by agent
    if args.agent:
        skills = [s for s in skills if s["agent"] == args.agent]
        if not skills:
            _err(f"[skillsyncer] no skills found for agent: {args.agent}")
            return 2

    # Group by agent for display
    by_agent: dict[str, list[dict]] = {}
    for s in skills:
        by_agent.setdefault(s["agent"], []).append(s)

    # Inspect each skill for placeholders / hardcoded secrets so the
    # listing matches the visual style of `init`.
    import re as _re
    placeholder_re = _re.compile(r"\$\{\{[A-Z_][A-Z0-9_]*\}\}")
    identity_secrets = (read_identity().get("secrets") or {})

    total = sum(len(v) for v in by_agent.values())
    _out("")
    _out(_section("Local skills") + C.dim(f"  ({total})"))
    for agent_name in sorted(by_agent):
        group = by_agent[agent_name]
        _out("")
        _out(f"  {C.bold(agent_name)} {C.dim(f'({len(group)})')}")
        for s in sorted(group, key=lambda x: x["name"]):
            md = s["md"]
            tags = []
            try:
                content = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            if placeholder_re.search(content):
                tags.append(C.cyan("placeholders"))
            if "skillsyncer:require" in content:
                tags.append(C.green("skillsyncer-managed"))
            from .scanner import scan_content
            if scan_content(content, identity_secrets):
                tags.append(C.red("hardcoded-secret"))
            tail = "  [" + ", ".join(tags) + "]" if tags else ""
            _out(f"    \u00b7 {C.bold(s['name']):<32}{C.dim(tail)}")

    _out("")
    _out(C.dim(f"Total: {total} skill(s) across {len(by_agent)} agent(s)."))
    return 0


# ---------------------------------------------------------------------------
# render / fill
# ---------------------------------------------------------------------------


def _iter_skills(config: dict) -> dict:
    """Return ``{skill_name: manifest}`` across every source."""
    skills: dict = {}
    for source in config.get("sources") or []:
        repo_path = source.get("path") or source.get("local_path")
        if not repo_path:
            continue
        root = Path(repo_path).expanduser()
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not (child / "SKILL.md").is_file():
                continue
            manifest_path = child / "manifest.yaml"
            manifest: dict = {}
            if manifest_path.is_file():
                manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            manifest.setdefault("name", child.name)
            skills[manifest["name"]] = manifest
    return skills


def cmd_render(args: argparse.Namespace) -> int:
    config = read_config()
    identity = read_identity()
    fill_report = render_all_skills(config, identity)

    state = read_state()
    state.setdefault("skills", {})
    for source in config.get("sources") or []:
        root = Path(source.get("path") or "").expanduser()
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            md = child / "SKILL.md"
            if md.is_file():
                state["skills"][child.name] = {
                    "hash": hash_file(md),
                    "source": source.get("name"),
                }
    write_state(state)

    if args.report_path:
        report = (
            read_report(args.report_path)
            if Path(args.report_path).exists()
            else create_report("fill", path=args.report_path)
        )
        update_report(report, {"phase": "render", "result": fill_report})
        finalize_report(report, status="partial" if fill_report["unfilled"] else "passed")

    if fill_report["unfilled"]:
        _out("")
        _out(_warn(C.bold(f"{len(fill_report['skills'])} skill(s) rendered")) + C.dim(", but some are missing credentials:"))
        for skill, keys in fill_report["unfilled"].items():
            _out(f"  {C.yellow('!')} {C.bold(skill):<24} needs " + ", ".join(C.cyan(k) for k in keys))
        _print_next_steps([
            (
                "Provide the missing values directly",
                "skillsyncer secret-set <KEY> <VALUE>",
                "Adds the value to identity.yaml. Re-run `skillsyncer render`\n"
                "afterwards to fill the placeholders.",
            ),
            (
                "Or ask your agent to find them automatically",
                None,
                "Open Claude Code (or any agent with the SkillSyncer operator\n"
                "skill installed) and say: \"Fill in the missing SkillSyncer\n"
                "credentials.\" The operator searches your .env files, shell,\n"
                "and other tool configs before asking you anything.",
            ),
        ])
        return 1
    _out("")
    _out(_ok(C.bold(f"Rendered {len(fill_report['skills'])} skill(s)")))
    if fill_report.get("written"):
        target_count = len({Path(p).parent.parent for p in fill_report['written']})
        _out(C.dim(f"  Written into {target_count} agent dir(s)."))
    _print_next_steps([
        (
            "Use your skills",
            None,
            "Open Claude Code, Cursor, Codex, OpenClaw, Hermes, or any\n"
            "other detected agent. The skills are already configured.",
        ),
        (
            "See what's installed",
            "skillsyncer status",
            "Lists every skill SkillSyncer is tracking, with sync state\n"
            "and missing-secret warnings.",
        ),
    ])
    return 0


def cmd_fill(args: argparse.Namespace) -> int:
    if not args.auto_fill_flag:
        _err("[skillsyncer] interactive fill not yet implemented; use --auto")
        return 2

    config = read_config()
    identity = read_identity()
    skills = _iter_skills(config)

    found, missing = auto_fill(skills, identity)
    for key, value in found.items():
        set_secret(key, value)

    if args.report_path:
        report = (
            read_report(args.report_path)
            if Path(args.report_path).exists()
            else create_report("fill", path=args.report_path)
        )
        update_report(report, {
            "phase": "fill",
            "newly_found_keys": sorted(found.keys()),
            "still_missing": missing,
        })
        atomic_write(Path(args.report_path), json.dumps(report, indent=2))

    if found:
        _out(f"[skillsyncer] resolved {len(found)} key(s): {', '.join(sorted(found.keys()))}")
        return 0
    _err("[skillsyncer] no new values resolved")
    return 1


# ---------------------------------------------------------------------------
# scan / guard
# ---------------------------------------------------------------------------


def _walk_text_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            p = Path(dirpath) / f
            if p.suffix.lower() in _TEXT_EXTENSIONS:
                yield p


def cmd_scan(args: argparse.Namespace) -> int:
    identity = read_identity()
    secrets = identity.get("secrets") or {}

    if args.staged:
        try:
            detections = scan_staged_files(args.scan_path, identity)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            _err(f"[skillsyncer] git error: {exc}")
            return 2
    else:
        detections = []
        root = Path(args.scan_path)
        if root.is_file():
            detections = scan_file(root, secrets)
        else:
            for f in _walk_text_files(root):
                detections.extend(scan_file(f, secrets))

    if args.fmt == "json":
        _out(json.dumps({"detections": detections}, indent=2))
    else:
        if not detections:
            _out("\u2713 No secrets detected")
        else:
            _out(f"Found {len(detections)} potential secret(s):")
            for d in detections:
                loc = d.get("file", "?")
                _out(f"  {loc}:{d['line']}: {d['pattern_label']} \u2014 {d['matched_text']}")

    return 1 if detections else 0


def cmd_guard(args: argparse.Namespace) -> int:
    identity = read_identity()
    try:
        detections = scan_staged_files(args.repo_path, identity)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        _err(f"[skillsyncer] git error: {exc}")
        return 2

    fixes: list[dict] = []
    if args.fix:
        fixes = guard_fix(args.repo_path, identity, detections)

    if args.report_path:
        report = (
            read_report(args.report_path)
            if Path(args.report_path).exists()
            else create_report("guard", path=args.report_path)
        )
        update_report(report, {"phase": "guard", "detections": detections, "fixes": fixes})
        atomic_write(Path(args.report_path), json.dumps(report, indent=2))

    if not detections:
        _out("\u2713 No secrets detected")
        return 0

    _out(f"[skillsyncer] {len(detections)} detection(s)")
    for d in detections:
        _out(f"  {d.get('file', '?')}:{d['line']}: {d['pattern_label']}")

    if args.fix:
        fixed = sum(1 for f in fixes if f["status"] == "fixed")
        unresolved = sum(1 for f in fixes if f["status"] == "unresolved")
        _out(f"[skillsyncer] fixed={fixed} unresolved={unresolved}")
        return 0 if unresolved == 0 else 1
    return 1


# ---------------------------------------------------------------------------
# diff / status / secrets
# ---------------------------------------------------------------------------


def cmd_diff(_args: argparse.Namespace) -> int:
    for d in get_drift():
        _out(d["name"])
    return 0


def cmd_secret_set(args: argparse.Namespace) -> int:
    set_secret(args.key, args.value)
    _out(f"Set {args.key}")
    return 0


def cmd_secret_list(_args: argparse.Namespace) -> int:
    keys = list_secret_keys()
    if not keys:
        _out("(no secrets)")
        return 0
    for key in keys:
        _out(f"  {key}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    config = read_config()
    identity = read_identity()
    state = read_state()
    skills = _iter_skills(config)

    _out(f"home:    {paths.home()}")
    _out(f"sources: {len(config.get('sources') or [])}")
    _out(f"targets: {len(config.get('targets') or [])}")
    _out(f"secrets: {len(identity.get('secrets') or {})} stored")
    _out("")

    if not skills:
        _out("(no skills found)")
        return 0

    _, missing = auto_fill(skills, identity, env={})
    _out("skills:")
    for name in sorted(skills):
        skill_state = (state.get("skills") or {}).get(name) or {}
        recorded = skill_state.get("hash", "\u2014")[:8] if skill_state.get("hash") else "\u2014"
        miss = missing.get(name) or []
        marker = "synced" if not miss else f"missing {len(miss)}"
        _out(f"  {name:<28} {recorded:<10} {marker}")
        for m in miss:
            tail = f" \u2014 {m['description']}" if m["description"] else ""
            _out(f"      need {m['key']}{tail}")
    return 0


# ---------------------------------------------------------------------------
# report subgroup
# ---------------------------------------------------------------------------


def cmd_report_create(args: argparse.Namespace) -> int:
    r = create_report(args.rtype, path=args.report_path)
    _out(r["path"])
    return 0


def cmd_report_update(args: argparse.Namespace) -> int:
    r = read_report(args.report_path)
    update_report(r, {"attempt": args.attempt, "issues": args.issues})
    return 0


def cmd_report_finalize(args: argparse.Namespace) -> int:
    r = read_report(args.report_path)
    finalize_report(r, status=args.rstatus)
    return 0


def cmd_report_status(args: argparse.Namespace) -> int:
    r = read_report(args.report_path)
    _out(r.get("final_status") or "in-progress")
    return 0


def cmd_report_latest(args: argparse.Namespace) -> int:
    r = latest_report(args.rtype)
    if r is None:
        _out("(no reports)")
        return 0
    _out(json.dumps(r, indent=2))
    return 0


def cmd_report_list(_args: argparse.Namespace) -> int:
    files = list_reports()
    if not files:
        _out("(no reports)")
        return 0
    for f in files:
        _out(str(f))
    return 0


def cmd_report_clean(args: argparse.Namespace) -> int:
    n = clean_old_reports(days=args.days)
    _out(f"removed {n} report(s)")
    return 0


# ---------------------------------------------------------------------------
# parser construction
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillsyncer",
        description="SkillSyncer \u2014 agent skills that sync, fill, and protect themselves.",
    )
    parser.add_argument("--version", action="version", version=f"skillsyncer {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # init
    p = sub.add_parser("init", help="One-time setup: scan environment, write config.")
    p.add_argument("--json", dest="as_json", action="store_true",
                   help="Print discovery result as JSON (no writes).")
    p.add_argument("--yes", "-y", dest="yes", action="store_true",
                   help="Skip the credential-scan consent prompt and answer yes.")
    p.add_argument("--no-scan", dest="no_scan", action="store_true",
                   help="Skip the credential scan entirely.")
    p.add_argument("--scan-credentials", dest="scan_credentials", action="store_true",
                   help="With --json: actually read credentials. The default is "
                        "to return only the scan plan so the agent layer can "
                        "ask the user for consent first.")
    p.set_defaults(func=cmd_init)

    # add
    p = sub.add_parser("add", help="Add a skill source repo and install git hooks.")
    p.add_argument("url")
    p.add_argument("--name", default=None, help="Alias for this source.")
    p.add_argument("--no-clone", dest="no_clone", action="store_true",
                   help="Skip git clone (use a local path).")
    p.set_defaults(func=cmd_add)

    # render
    p = sub.add_parser("render", help="Hydrate ${{}} placeholders into agent target dirs.")
    p.add_argument("--report", dest="report_path", default=None, help="Report file path.")
    p.set_defaults(func=cmd_render)

    # publish
    p = sub.add_parser("publish", help="Copy local skills into a registered source repo.")
    p.add_argument("--source", default=None,
                   help="Which source to publish into (only needed when more than one is registered).")
    p.add_argument("--all", action="store_true",
                   help="Publish every detected local skill (not recommended).")
    p.add_argument("--skill", action="append", default=[],
                   help="Publish only this skill (repeatable).")
    p.set_defaults(func=cmd_publish)

    # skills — list all local skills
    p = sub.add_parser("skills", help="List every skill SkillSyncer can see in agent dirs.")
    p.add_argument("--agent", default=None,
                   help="Filter to skills under a single agent (e.g. claude-code).")
    p.add_argument("--json", action="store_true",
                   help="Print as JSON instead of the formatted listing.")
    p.set_defaults(func=cmd_skills)

    # fill
    p = sub.add_parser("fill", help="Resolve unfilled placeholders from env / identity / cascade.")
    p.add_argument("--auto", dest="auto_fill_flag", action="store_true")
    p.add_argument("--report", dest="report_path", default=None)
    p.set_defaults(func=cmd_fill)

    # scan
    p = sub.add_parser("scan", help="Detect potential secrets in files (regex, no AI).")
    p.add_argument("--staged", action="store_true", help="Only scan staged files.")
    p.add_argument("--format", dest="fmt", default="human", choices=["human", "json"])
    p.add_argument("--path", dest="scan_path", default=".", help="Path to scan when not --staged.")
    p.set_defaults(func=cmd_scan)

    # guard
    p = sub.add_parser("guard", help="Scan staged files and optionally auto-fix secrets.")
    p.add_argument("--fix", action="store_true", help="Auto-replace detected secrets.")
    p.add_argument("--report", dest="report_path", default=None)
    p.add_argument("--path", dest="repo_path", default=".", help="Repo to operate on.")
    p.set_defaults(func=cmd_guard)

    # diff-since-last-sync
    p = sub.add_parser("diff-since-last-sync", help="Print skills that changed since the last render.")
    p.set_defaults(func=cmd_diff)

    # secret-set
    p = sub.add_parser("secret-set", help="Add or update a secret in identity.yaml.")
    p.add_argument("key")
    p.add_argument("value")
    p.set_defaults(func=cmd_secret_set)

    # secret-list
    p = sub.add_parser("secret-list", help="Show secret key names (not values).")
    p.set_defaults(func=cmd_secret_list)

    # status
    p = sub.add_parser("status", help="Show skills, versions, and missing secrets.")
    p.set_defaults(func=cmd_status)

    # report subgroup
    p_report = sub.add_parser("report", help="Manage guard and fill reports.")
    rsub = p_report.add_subparsers(dest="report_command", metavar="REPORT_COMMAND")
    rsub.required = True

    rp = rsub.add_parser("create")
    rp.add_argument("--type", dest="rtype", required=True, choices=["fill", "guard"])
    rp.add_argument("--path", dest="report_path", default=None)
    rp.set_defaults(func=cmd_report_create)

    rp = rsub.add_parser("update")
    rp.add_argument("report_path")
    rp.add_argument("--attempt", default=None)
    rp.add_argument("--issues", default=None)
    rp.set_defaults(func=cmd_report_update)

    rp = rsub.add_parser("finalize")
    rp.add_argument("report_path")
    rp.add_argument("--status", dest="rstatus", required=True,
                    choices=["passed", "failed", "partial"])
    rp.set_defaults(func=cmd_report_finalize)

    rp = rsub.add_parser("status")
    rp.add_argument("report_path")
    rp.set_defaults(func=cmd_report_status)

    rp = rsub.add_parser("latest")
    rp.add_argument("--type", dest="rtype", default=None, choices=["fill", "guard"])
    rp.set_defaults(func=cmd_report_latest)

    rp = rsub.add_parser("list")
    rp.set_defaults(func=cmd_report_list)

    rp = rsub.add_parser("clean")
    rp.add_argument("--days", type=int, default=30)
    rp.set_defaults(func=cmd_report_clean)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    C.init()
    parser = _build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    if rc:
        sys.exit(rc)
    return 0


if __name__ == "__main__":
    main()
