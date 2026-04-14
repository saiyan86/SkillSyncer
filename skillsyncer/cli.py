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
import threading
import time
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


class _Spinner:
    """Context-manager spinner for blocking operations.

    Shows an animated indicator on stderr while work runs on the main
    thread. Degrades gracefully: if stderr isn't a TTY (CI, pipes) it
    prints a plain "msg..." line instead of animating.

    Usage::

        with _Spinner("cloning YCSkills"):
            subprocess.run(["git", "clone", ...], check=True)
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, msg: str) -> None:
        self._msg = msg
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "_Spinner":
        if sys.stderr.isatty():
            self._thread.start()
        else:
            sys.stderr.write(f"  {self._msg}…\n")
            sys.stderr.flush()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        if sys.stderr.isatty():
            # Erase the spinner line.
            sys.stderr.write("\r" + " " * (len(self._msg) + 6) + "\r")
            sys.stderr.flush()

    def _run(self) -> None:
        i = 0
        while not self._stop.wait(0.1):
            frame = self._FRAMES[i % len(self._FRAMES)]
            sys.stderr.write(f"\r  {frame} {self._msg}…")
            sys.stderr.flush()
            i += 1


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
GLYPH_TRI = "\u25b8"        # ▸
GLYPH_SQ = "\u25a3"         # ▣
GLYPH_PROMPT = "\u25b6"     # ▶
BOX_HEAVY = "\u2500" * 60   # ─ × 60
# Light rounded box (banner / Next-steps)
BOX_TL = "\u256d"           # ╭
BOX_TR = "\u256e"           # ╮
BOX_BL = "\u2570"           # ╰
BOX_BR = "\u256f"           # ╯
BOX_V = "\u2502"            # │
# Heavy box for the high-attention consent prompt
BOX_TL_HV = "\u250f"        # ┏
BOX_TR_HV = "\u2513"        # ┓
BOX_BL_HV = "\u2517"        # ┗
BOX_BR_HV = "\u251b"        # ┛
BOX_V_HV = "\u2503"         # ┃
BOX_H_HV = "\u2501"         # ━


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

    Heavy-bordered, colored consent screen so it's visually clear
    that the user is being asked to approve a security-sensitive
    operation. The bar on the left is bold yellow (the "attention"
    color), section labels are bold cyan, ✓ marks are green, and
    the security promise at the bottom is bold green.

    Returns True for yes (default), False for no. On non-interactive
    stdin, defaults to False so a curl-piped invocation never
    silently reads files the user didn't approve.
    """
    existing = [p for p in plan if p["exists"]]
    skipped = len(plan) - len(existing)

    bar = C.yellow(BOX_V_HV)
    width = 64
    title = " Credential scan consent "
    title_pad = (width - len(title) - 2) * BOX_H_HV
    top = C.yellow(BOX_TL_HV + BOX_H_HV + BOX_H_HV) + " " + C.bold(C.yellow(title.strip())) + " " + C.yellow(BOX_H_HV * (width - len(title) - 4))
    bot = C.yellow(BOX_BL_HV + BOX_H_HV * (width - 1))

    _out("")
    _out(top)
    _out(bar)
    _out(bar + "  " + C.bold("SkillSyncer would like to read these locations to find"))
    _out(bar + "  " + C.bold("credentials it can pre-fill in your skills:"))
    _out(bar)

    by_kind: dict[str, list[dict]] = {}
    for entry in existing:
        by_kind.setdefault(entry["kind"], []).append(entry)

    LABELS = {
        "project": "Project (./)",
        "home": "User home (~)",
        "shell": "Shell environment",
        "ai-tool": "AI tool config dirs",
    }
    for kind in ("project", "home", "shell", "ai-tool"):
        if kind not in by_kind:
            continue
        _out(bar + "  " + C.cyan(GLYPH_TRI) + " " + C.bold(C.cyan(LABELS[kind])))
        for e in by_kind[kind]:
            _out(bar + "      " + C.green(GLYPH_CHECK) + " " + e["display"])
        _out(bar)

    if skipped:
        _out(bar + "  " + C.dim(f"({skipped} additional locations checked but not present)"))
        _out(bar)
    _out(bar + "  " + C.bold(C.green(GLYPH_SQ + "  Values stay on this machine.")))
    _out(bar + "     " + C.dim("Only KEY NAMES are ever printed \u2014 never the values."))
    _out(bar)
    _out(bot)
    _out("")

    if not sys.stdin.isatty():
        _err(C.yellow("[skillsyncer] non-interactive shell \u2014 skipping credential scan."))
        _err(C.dim("[skillsyncer] re-run `skillsyncer init --yes` to scan, or"))
        _err(C.dim("[skillsyncer] `skillsyncer init --no-scan` to silence this notice."))
        return False

    prompt = (
        "  " + C.bold(C.yellow(GLYPH_PROMPT))
        + "  " + C.bold("Scan these locations now?")
        + "  " + C.dim("[Y/n] ")
    )
    try:
        answer = input(prompt).strip().lower()
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
        with _Spinner("scanning your environment"):
            preview = discover(scan_credentials=False)
        scan_creds = _consent_prompt(preview["credential_scan_plan"])

    with _Spinner("reading credentials"):
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

    # When run interactively (no flags, not piped) continue into the wizard
    # so the user never has to type follow-up commands. The JSON / --yes /
    # --no-scan paths are headless — keep their existing next-steps output.
    if sys.stdin.isatty() and not args.as_json:
        _wizard_continue(proposal)
    else:
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


# ---------------------------------------------------------------------------
# onboard — interactive setup wizard
# ---------------------------------------------------------------------------

_BOX_H = "\u2500"           # ─  (light horizontal, for wizard step dividers)


def _onboard_step(n: int, total: int, title: str) -> None:
    """Print a coloured step header: ─── Step n/total · title ─────────────"""
    label = f" Step {n}/{total} \u00b7 {title} "
    left = 3
    right = max(0, 58 - left - len(label))
    _out("")
    _out("  " + C.bold(C.cyan(_BOX_H * left + label + _BOX_H * right)))


def _wizard_continue(proposal: dict) -> None:
    """Steps 2–4 of the setup wizard: source repo → render → publish → done.

    Shared between ``cmd_onboard`` (which runs all 4 steps) and the
    interactive path of ``cmd_init`` (which has already done step 1).
    """
    TOTAL_STEPS = 4

    # ── Step 2: Source repo ────────────────────────────────────────────────────
    _onboard_step(2, TOTAL_STEPS, "Connect a skills repo (optional)")

    config = read_config()
    existing_sources = config.get("sources") or []
    gh_ok = bool(proposal.get("git", {}).get("gh_authenticated"))

    _out("")
    if existing_sources:
        _out(C.dim("  Already connected: " + ", ".join(s["name"] for s in existing_sources)))
        _out(C.dim("  You can add another or skip."))
    else:
        _out("  A Git repo lets you sync skills across machines and with teammates.")
    _out("")

    choices: list[tuple[str, str]] = []
    if gh_ok:
        choices.append(("a", "Create a new private GitHub repo"))
    choices.append(("b", "Use an existing repo (paste URL)"))
    choices.append(("s", "Skip for now"))

    for key, label in choices:
        marker = C.bold(C.yellow(f"[{key.upper()}]"))
        _out(f"  {marker}  {label}")
    _out("")

    if not sys.stdin.isatty():
        _out(C.dim("  (non-interactive \u2014 skipping source repo setup)"))
        source_choice = "s"
    else:
        valid_keys = {k for k, _ in choices}
        while True:
            keys_hint = "/".join(k.upper() for k, _ in choices)
            try:
                raw = input(
                    f"  {C.bold(C.yellow(GLYPH_PROMPT))}  Choice [{keys_hint}]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                _out("")
                raw = "s"
            if not raw:
                source_choice = "s"
                break
            if raw in valid_keys:
                source_choice = raw
                break
            _out(f"  Please enter one of: {keys_hint}")

    if source_choice == "a" and gh_ok:
        _out("")
        try:
            raw_name = input(
                f"  {C.bold(C.yellow(GLYPH_PROMPT))}  Repo name [agent-skills]: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            raw_name = ""
        repo_name = raw_name or "agent-skills"

        with _Spinner(f"creating github.com/{repo_name}"):
            gh_result = subprocess.run(
                ["gh", "repo", "create", repo_name, "--private"],
                capture_output=True, text=True, check=False,
            )

        if gh_result.returncode != 0:
            _out(C.red(f"  {GLYPH_CROSS} gh repo create failed: {gh_result.stderr.strip()}"))
            _out(C.dim("  Run `skillsyncer add <url>` manually after creating the repo."))
        else:
            repo_url = gh_result.stdout.strip()
            # gh outputs the HTTPS URL; prefer SSH
            if repo_url.startswith("https://github.com/"):
                repo_path = repo_url.removeprefix("https://github.com/")
                repo_url = f"git@github.com:{repo_path}.git"
            _out(C.dim(f"  {GLYPH_ARROW} {repo_url}"))
            add_args = argparse.Namespace(url=repo_url, name=None, no_clone=False)
            cmd_add(add_args)

    elif source_choice == "b":
        _out("")
        try:
            repo_url = input(
                f"  {C.bold(C.yellow(GLYPH_PROMPT))}  Repo URL: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            repo_url = ""
        if repo_url:
            add_args = argparse.Namespace(url=repo_url, name=None, no_clone=False)
            cmd_add(add_args)
        else:
            _out(C.dim("  No URL entered \u2014 skipped."))

    else:
        _out(C.dim("  Skipped. Run `skillsyncer add <url>` whenever you\u2019re ready."))

    # ── Step 3: Render ─────────────────────────────────────────────────────────
    _onboard_step(3, TOTAL_STEPS, "Hydrate placeholders into your agent dirs")

    config = read_config()
    if not config.get("sources"):
        _out("")
        _out(C.dim("  No source repos registered \u2014 nothing to render yet."))
        _out(C.dim("  Run `skillsyncer render` after adding a source."))
    else:
        render_args = argparse.Namespace(report_path=None)
        cmd_render(render_args)

    # ── Step 4: Publish ────────────────────────────────────────────────────────
    _onboard_step(4, TOTAL_STEPS, "Publish your skills to the repo (optional)")

    config = read_config()
    has_source = bool(config.get("sources"))
    has_local_skills = bool(proposal.get("existing_skills"))

    if not has_source:
        _out("")
        _out(C.dim("  No source repo \u2014 skipping. Connect one first with `skillsyncer add`."))
    elif not has_local_skills:
        _out("")
        _out(C.dim("  No local skills found \u2014 nothing to publish yet."))
    elif not sys.stdin.isatty():
        _out("")
        _out(C.dim("  (non-interactive \u2014 skipping publish)"))
    else:
        _out("")
        _out("  Copy your local skills into the connected repo so teammates")
        _out("  (and your other machines) can pull them in.")
        _out("")
        _out(f"  {C.bold(C.yellow('[Y]'))}  Publish skills now")
        _out(f"  {C.bold(C.yellow('[S]'))}  Skip for now")
        _out("")
        try:
            raw = input(
                f"  {C.bold(C.yellow(GLYPH_PROMPT))}  Choice [Y/S]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            _out("")
            raw = "s"
        if raw in ("y", "yes", ""):
            publish_args = argparse.Namespace(source=None, all=False, skill=[])
            cmd_publish(publish_args)
        else:
            _out(C.dim("  Skipped. Run `skillsyncer publish` whenever you\u2019re ready."))

    # ── Done ───────────────────────────────────────────────────────────────────
    done_text = f"  {C.green(GLYPH_CHECK)}  {C.bold('Setup complete')}"
    inner_width = 56
    _out("")
    _out(C.cyan("  " + BOX_TL + _BOX_H * inner_width + BOX_TR))
    _out(C.cyan("  " + BOX_V) + done_text + C.cyan(BOX_V))
    _out(C.cyan("  " + BOX_BL + _BOX_H * inner_width + BOX_BR))
    _out("")
    _out(C.dim("  skillsyncer status   \u2014 health check"))
    _out(C.dim("  skillsyncer skills   \u2014 list installed skills"))
    _out(C.dim("  skillsyncer publish  \u2014 share more skills upstream"))
    _out("")


def cmd_onboard(_args: argparse.Namespace) -> int:
    """Interactive setup wizard: init → source repo → render → publish."""

    home = paths.home()
    already_init = (home / "config.yaml").exists()
    TOTAL_STEPS = 4

    # ── Welcome ───────────────────────────────────────────────────────────────
    _print_banner()
    if already_init:
        _out(C.dim(f"  Re-running onboarding. Existing config at {home}"))
        _out(C.dim("  Nothing is wiped \u2014 we\u2019ll update what\u2019s needed."))
    else:
        _out(C.bold("  Let\u2019s get you set up in four steps."))
    _out("")

    # ── Step 1: Credential scan ───────────────────────────────────────────────
    _onboard_step(1, TOTAL_STEPS, "Scan your environment")

    home.mkdir(parents=True, exist_ok=True)

    with _Spinner("probing your environment"):
        preview = discover(scan_credentials=False)

    scan_creds = _consent_prompt(preview["credential_scan_plan"])

    with _Spinner("reading credentials" if scan_creds else "initialising"):
        proposal = discover(scan_credentials=scan_creds)

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

    # Discovery summary
    found_agents = [a for a in proposal["agents"] if a["found"]]
    _out("")
    if found_agents:
        _out(_section("Agents") + C.dim(f"  ({len(found_agents)})"))
        for a in found_agents:
            _out(f"  {C.green(GLYPH_CHECK)} {C.bold(a['name']):<22} {C.dim(a['path'])}")
    else:
        _out(_section("Agents") + C.dim("  (none detected)"))

    if proposal["existing_skills"]:
        SKILLS_PER_AGENT = 6
        by_agent_skills: dict[str, list[dict]] = {}
        for s in proposal["existing_skills"]:
            by_agent_skills.setdefault(s["agent"], []).append(s)
        _out("")
        _out(_section("Skills") + C.dim(f"  ({len(proposal['existing_skills'])})"))
        for agent_name in sorted(by_agent_skills):
            group = by_agent_skills[agent_name]
            _out(f"  {C.bold(agent_name)} {C.dim(f'({len(group)})')}")
            for s in group[:SKILLS_PER_AGENT]:
                tags = []
                if s["has_placeholders"]:
                    tags.append(C.cyan("placeholders"))
                if s["has_hardcoded_secrets"]:
                    tags.append(C.red("hardcoded-secret"))
                tail = f"  [{', '.join(tags)}]" if tags else ""
                _out(f"    {C.dim(GLYPH_BULLET)} {s['name']}{tail}")
            if len(group) > SKILLS_PER_AGENT:
                _out(C.dim(f"    {GLYPH_BULLET} \u2026 and {len(group) - SKILLS_PER_AGENT} more"))

    if proposal.get("credential_scan_performed") and proposal["credentials"]:
        by_cred_key: dict[str, list[dict]] = {}
        for c in proposal["credentials"]:
            by_cred_key.setdefault(c["key"], []).append(c)
        _out("")
        _out(_section("Credentials") + C.dim(f"  ({len(by_cred_key)} found)"))
        for key in sorted(by_cred_key):
            cands = by_cred_key[key]
            src = sorted({c["source"] for c in cands})[0]
            _out(f"  {C.green(GLYPH_CHECK)}  {C.bold(key):<32} {C.dim(src)}")
    elif not proposal.get("credential_scan_performed"):
        _out("")
        _out(C.dim("  Credential scan skipped. Run `skillsyncer init --yes` to scan later."))

    _wizard_continue(proposal)
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
            with _Spinner(f"pulling latest {name}"):
                subprocess.run(
                    ["git", "-C", str(target), "pull", "--ff-only"],
                    check=False,
                    capture_output=True,
                )
        else:
            with _Spinner(f"cloning {url}"):
                try:
                    subprocess.run(
                        ["git", "clone", "--quiet", url, str(target)],
                        check=True,
                        capture_output=True,
                    )
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


# Directories that look like skill content but are actually build artifacts,
# vendored deps, or VCS metadata. Skipped both when copying a skill into a
# source repo and when scanning the copy for secrets — otherwise a skill that
# happens to be a full Node/Python project drags in node_modules, dist
# binaries, and .git internals (slow scan, huge false-positive count).
_SKIP_DIRS = frozenset({
    ".git",
    "node_modules",
    "dist",
    "build",
    "out",
    "target",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".next",
    ".cache",
})


def _iter_skill_files(root: Path):
    """Yield (abs_path, rel_path) for every real file under ``root``,
    pruning ``_SKIP_DIRS`` in place so we never descend into them."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        base = Path(dirpath)
        for name in filenames:
            full = base / name
            yield full, full.relative_to(root)


def _copy_skill_tree(src: Path, dst: Path) -> None:
    """Copy every file under ``src`` into ``dst``, skipping build
    artifacts and vendored deps. Overwrites individual files but
    leaves other files in ``dst`` untouched."""
    import shutil

    dst.mkdir(parents=True, exist_ok=True)
    for src_file, rel in _iter_skill_files(src):
        dst_file = dst / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)


def _skill_upstream(skill_dir: Path) -> str | None:
    """If the skill dir is itself a git repo with an ``origin`` remote,
    return the remote URL. Otherwise return None.

    This is the signal we use to offer "vendor vs reference" at
    publish time: a skill that has its own upstream is independently
    versioned and probably shouldn't be copy-pasted into the source
    repo wholesale.
    """
    if not (skill_dir / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(skill_dir), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    return url or None


def _ask_publish_mode(skill_name: str, upstream: str) -> str:
    """Ask the user how to publish a skill that has its own upstream.

    Returns one of ``vendor`` / ``reference`` / ``skip``. Falls back
    to ``vendor`` (current behavior) when stdin isn't a TTY so
    ``--all`` in CI / piped contexts stays predictable.
    """
    if not sys.stdin.isatty():
        _out("")
        _out(f"  ! {skill_name} is its own git repo (origin: {upstream})")
        _out("    non-interactive shell — defaulting to vendor (full copy).")
        _out("    Re-run interactively to publish as a reference instead.")
        return "vendor"

    _out("")
    _out(f"  {C.bold(C.yellow(skill_name))} is its own independently versioned project.")
    _out(f"  {C.dim('origin: ' + upstream)}")
    _out("")
    _out(  "  Vendoring it copies the full source tree into your skills repo —")
    _out(  "  every build artifact, test fixture, and binary. That's the source")
    _out(  "  of most secret-scanner false positives and inflated repo size.")
    _out(  "  A reference stub is usually the right call for a skill like this.")
    _out("")
    _out(f"    {C.bold('vendor')}     copy the full tree (frozen snapshot, fully auditable)")
    _out(f"    {C.bold('reference')}  one-line stub pointing teammates at the upstream")
    _out(f"    {C.bold('skip')}       leave this skill out of the publish entirely")
    _out("")
    while True:
        try:
            answer = input(
                f"  How to publish {skill_name}? [v]endor / [r]eference / [s]kip (default r): "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            _out("")
            return "skip"
        if answer in ("r", "ref", "reference", ""):
            return "reference"
        if answer in ("v", "vendor"):
            return "vendor"
        if answer in ("s", "skip"):
            return "skip"
        _out("    please answer v / r / s")


def _write_reference_stub(
    dst_dir: Path,
    name: str,
    agent: str,
    upstream: str,
) -> None:
    """Write a SKILL.md stub that points teammates' agents at the
    upstream repo instead of vendoring the skill tree.

    If the destination already contains files from a prior vendored
    publish, wipe them first — otherwise the stub coexists with the
    old tree and the source repo ends up with both.
    """
    import shutil

    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    md = dst_dir / "SKILL.md"
    body = (
        f"---\n"
        f"name: {name}\n"
        f"skillsyncer:reference: true\n"
        f"upstream: {upstream}\n"
        f"agent: {agent}\n"
        f"---\n\n"
        f"# {name}\n\n"
        f"This is a SkillSyncer **reference**, not a vendored skill.\n\n"
        f"The full skill content lives upstream at:\n\n"
        f"    {upstream}\n\n"
        f"To install it on your machine, clone the upstream repo into\n"
        f"your agent's skills directory:\n\n"
        f"    git clone {upstream} ~/.{agent}/skills/{name}\n\n"
        f"After cloning, this stub is replaced by the real skill.\n\n"
        f"SkillSyncer chose not to vendor the upstream files into this\n"
        f"source repo because the skill is independently versioned\n"
        f"upstream. To freeze a snapshot instead, re-run\n"
        f"`skillsyncer publish` and choose `vendor` for this skill.\n"
    )
    atomic_write(md, body)


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

    _out(f"\n[skillsyncer] publishing {len(selected)} skill(s) into {target['name']}\u2026")
    copied: list[dict] = []        # vendored — full file copy
    referenced: list[dict] = []    # stub pointing at upstream
    for skill in selected:
        dst_dir = target_path / skill["name"]
        upstream = _skill_upstream(skill["dir"])
        mode = "vendor"
        if upstream:
            mode = _ask_publish_mode(skill["name"], upstream)

        if mode == "skip":
            _out(f"  \u00b7 {skill['name']:<28} (skipped)")
            continue

        if mode == "reference":
            _write_reference_stub(dst_dir, skill["name"], skill["agent"], upstream)
            _inject_preamble_if_missing(dst_dir / "SKILL.md")
            referenced.append({**skill, "upstream": upstream})
            _out(f"  \u2192 {skill['name']:<28} (reference \u2192 {upstream})")
            continue

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
        for f, _ in _iter_skill_files(dst_dir):
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

    published = copied + referenced
    if not published:
        _out("\n[skillsyncer] nothing to commit (everything was skipped).")
        return 0

    # Stage + commit (do not push — that's on the user, and the
    # pre-push hook will run a final security scan).
    msg_lines = [
        f"Publish {len(published)} skill(s) via SkillSyncer",
        "",
    ]
    for s in copied:
        msg_lines.append(f"- {s['name']}")
    for s in referenced:
        msg_lines.append(f"- {s['name']} (reference \u2192 {s['upstream']})")
    msg = "\n".join(msg_lines)

    # Stage only the skill paths this run actually touched (one
    # top-level dir per published skill). ``--all`` scoped to those
    # paths picks up additions, modifications, AND deletions inside
    # them — important when switching a skill from vendor → reference,
    # which wipes the old tree. Skipped skills, and any unrelated
    # dirty files in the source repo, are intentionally left alone.
    touched_paths = [s["name"] for s in published]
    try:
        subprocess.run(
            ["git", "-C", str(target_path), "add", "--all", "--", *touched_paths],
            check=True,
        )
        diff = subprocess.run(
            ["git", "-C", str(target_path), "diff", "--cached", "--quiet", "--", *touched_paths],
            check=False,
        )
        if diff.returncode == 0:
            _out("\n[skillsyncer] no changes to commit (skills already match the source).")
            return 0
        subprocess.run(
            ["git", "-C", str(target_path), "commit", "-m", msg, "--", *touched_paths],
            check=True,
        )
        push_result = subprocess.run(
            ["git", "-C", str(target_path), "push"],
            check=False,
        )
        push_ok = push_result.returncode == 0
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        _err(f"[skillsyncer] git error: {exc}")
        return 2

    # Credential-protection summary: count ${{...}} placeholders across
    # the vendored skills we just committed. Referenced stubs have none
    # by construction. This gives the user instant feedback on how many
    # credential values are shielded from the source repo.
    import re as _re
    _ph_pattern = _re.compile(r"\$\{\{([A-Z_][A-Z0-9_]*)\}\}")
    ph_counts: dict[str, int] = {}
    ph_files: set[str] = set()
    for s in copied:
        for f, _ in _iter_skill_files(target_path / s["name"]):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _ph_pattern.findall(text):
                ph_files.add(str(f))
                ph_counts[m] = ph_counts.get(m, 0) + 1

    _out("")
    action = "committed and pushed" if push_ok else "committed"
    _out(_ok(f"{len(published)} skill(s) {action} to {C.bold(target['name'])}"))
    if not push_ok:
        _out(C.dim(f"    (push failed — run `git -C {target_path} push` manually)"))
    for s in copied:
        _out(f"    {C.dim(GLYPH_BULLET)} {s['name']}")
    for s in referenced:
        _out(f"    {C.dim(GLYPH_BULLET)} {s['name']} {C.dim('(reference)')}")

    _out("")
    if ph_counts:
        total_ph = sum(ph_counts.values())
        _out(C.bold(
            f"\U0001f512 {len(ph_counts)} credential(s) shielded via "
            f"{total_ph} placeholder(s) across {len(ph_files)} file(s)"
        ))
        for name, n in sorted(ph_counts.items(), key=lambda x: -x[1])[:10]:
            _out(f"    {C.dim(GLYPH_BULLET)} ${{{{{name}}}}} \u00d7{n}")
    else:
        _out(C.dim(
            "\u26a0\ufe0f  0 credentials shielded — run `skillsyncer init` then "
            "`skillsyncer guard --fix` to template hardcoded values into placeholders."
        ))

    _print_next_steps([
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
# sync — git pull every source, then render
# ---------------------------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    config = read_config()
    sources = config.get("sources") or []
    if not sources:
        _err("[skillsyncer] no sources registered. Run `skillsyncer add <git-url>` first.")
        return 2

    _out("")
    _out(_section("Pulling sources") + C.dim(f"  ({len(sources)})"))
    pulled: list[str] = []
    failed: list[tuple[str, str]] = []
    for source in sources:
        name = source.get("name", "?")
        path = source.get("path") or ""
        repo = Path(path).expanduser()
        if not repo.is_dir():
            failed.append((name, "path missing"))
            _out(f"  {C.red(GLYPH_CROSS)} {C.bold(name):<20} {C.red('path missing')}")
            continue
        if not (repo / ".git").exists():
            failed.append((name, "not a git repo"))
            _out(f"  {C.red(GLYPH_CROSS)} {C.bold(name):<20} {C.red('not a git repo')}")
            continue
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), "pull", "--ff-only"],
                capture_output=True, text=True, check=True,
            )
            summary = proc.stdout.strip().splitlines()
            tail = summary[-1] if summary else "up to date"
            pulled.append(name)
            _out(f"  {C.green(GLYPH_CHECK)} {C.bold(name):<20} {C.dim(tail)}")
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            err = getattr(exc, "stderr", "").strip() or str(exc)
            failed.append((name, err))
            _out(f"  {C.red(GLYPH_CROSS)} {C.bold(name):<20} {C.red(err[:60])}")

    _out("")
    if not pulled:
        _err("[skillsyncer] nothing pulled.")
        return 1

    # Hand off to render
    render_args = argparse.Namespace(report_path=None)
    rc = cmd_render(render_args)
    return rc


# ---------------------------------------------------------------------------
# doctor — diagnose state and common problems
# ---------------------------------------------------------------------------


def cmd_doctor(_args: argparse.Namespace) -> int:
    successes: list[str] = []
    issues: list[str] = []

    # Toolchain
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
        successes.append(f"git: {proc.stdout.strip()}")
    except (FileNotFoundError, subprocess.CalledProcessError):
        issues.append("git is not on PATH — required for sources and hooks")

    try:
        import yaml as _yaml
        successes.append(f"pyyaml: {_yaml.__version__}")
    except ImportError:
        issues.append("pyyaml is not installed (the install script should have done this)")

    # Home + state files
    home = paths.home()
    if home.exists():
        successes.append(f"home: {home}")
    else:
        issues.append(f"home: {home} does not exist — run `skillsyncer init`")
        # No point continuing the per-file checks
        _print_doctor(successes, issues)
        return 1

    if paths.identity_path().exists():
        n_secrets = len(read_identity().get("secrets") or {})
        successes.append(f"identity.yaml: {n_secrets} secret(s) stored")
    else:
        issues.append("identity.yaml missing — run `skillsyncer init`")

    config = read_config()
    if paths.config_path().exists():
        n_sources = len(config.get("sources") or [])
        n_targets = len(config.get("targets") or [])
        successes.append(f"config.yaml: {n_sources} source(s), {n_targets} target(s)")
    else:
        issues.append("config.yaml missing — run `skillsyncer init`")

    state = read_state()
    n_state_skills = len(state.get("skills") or {})
    successes.append(f"state.yaml: {n_state_skills} skill(s) tracked")

    # Sources
    from .hooks import hook_is_installed
    for source in config.get("sources") or []:
        name = source.get("name", "?")
        path = source.get("path")
        if not path:
            issues.append(f"source '{name}': no path recorded")
            continue
        repo = Path(path).expanduser()
        if not repo.is_dir():
            issues.append(f"source '{name}': path doesn't exist ({repo})")
            continue
        if not (repo / ".git").exists():
            issues.append(f"source '{name}': not a git repo ({repo})")
            continue
        if not hook_is_installed(repo, "pre-push"):
            issues.append(
                f"source '{name}': pre-push hook missing — "
                f"run `skillsyncer hooks install --path {repo}`"
            )
        else:
            successes.append(f"source '{name}': hooks installed")

    # Targets
    for target in config.get("targets") or []:
        name = target.get("name", "?")
        path = target.get("path")
        if not path:
            continue
        target_dir = Path(path).expanduser()
        if target_dir.exists():
            successes.append(f"target '{name}': {target_dir}")
        else:
            issues.append(f"target '{name}': path doesn't exist ({target_dir})")

    # Local skills
    local_skills = _find_local_skills()
    successes.append(f"local skills: {len(local_skills)} discovered")

    _print_doctor(successes, issues)
    return 0 if not issues else 1


def _print_doctor(successes: list[str], issues: list[str]) -> None:
    _out("")
    _out(_section("SkillSyncer doctor") + C.dim(f"  ({len(successes)} ok, {len(issues)} issue(s))"))
    _out("")
    for s in successes:
        _out(f"  {C.green(GLYPH_CHECK)} {s}")
    for i in issues:
        _out(f"  {C.red(GLYPH_CROSS)} {i}")
    _out("")
    if not issues:
        _out(_ok(C.bold("Everything looks good.")))
    else:
        _out(_warn(C.bold(f"{len(issues)} issue(s) need attention.")))
    _out("")


# ---------------------------------------------------------------------------
# sources / hooks groups
# ---------------------------------------------------------------------------


def cmd_sources_list(_args: argparse.Namespace) -> int:
    config = read_config()
    sources = config.get("sources") or []
    if not sources:
        _out(C.dim("No sources registered."))
        return 0
    _out("")
    _out(_section("Sources") + C.dim(f"  ({len(sources)})"))
    for s in sources:
        name = s.get("name", "?")
        url = s.get("url", "?")
        path = s.get("path", "?")
        _out(f"  {C.bold(name)}")
        _out(f"    {C.dim('url:')}  {url}")
        _out(f"    {C.dim('path:')} {path}")
    return 0


def cmd_sources_remove(args: argparse.Namespace) -> int:
    config = read_config()
    sources = config.get("sources") or []
    match = next((s for s in sources if s.get("name") == args.name), None)
    if match is None:
        _err(f"[skillsyncer] source '{args.name}' not found.")
        return 2
    config["sources"] = [s for s in sources if s.get("name") != args.name]
    write_config(config)
    _out(_ok(f"Removed source {C.bold(args.name)}"))
    repo_path = match.get("path")
    if repo_path and Path(repo_path).exists():
        _out("")
        _out(C.dim(f"The cloned repo at {repo_path} was NOT deleted."))
        _out(C.dim(f"Remove it manually with: rm -rf {repo_path}"))
    return 0


def cmd_sources_show(args: argparse.Namespace) -> int:
    config = read_config()
    sources = config.get("sources") or []
    match = next((s for s in sources if s.get("name") == args.name), None)
    if match is None:
        _err(f"[skillsyncer] source '{args.name}' not found.")
        return 2
    _out("")
    _out(_section(f"Source: {match.get('name')}"))
    for k, v in match.items():
        _out(f"  {C.dim(k + ':'):<10} {v}")

    repo = Path(match.get("path") or "").expanduser()
    if repo.is_dir():
        _out("")
        _out(_section("Skills in this source"))
        skill_count = 0
        for child in sorted(repo.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            md = child / "SKILL.md"
            if md.is_file():
                skill_count += 1
                _out(f"  {GLYPH_BULLET} {child.name}")
        if not skill_count:
            _out(C.dim("  (no skills)"))
    return 0


def cmd_hooks_install(args: argparse.Namespace) -> int:
    repo = Path(args.path).resolve()
    try:
        written = hooks.install_hooks(repo)
    except FileNotFoundError as exc:
        _err(f"[skillsyncer] {exc}")
        return 2
    _out(_ok(f"Installed {len(written)} hook(s) into {repo}"))
    for w in written:
        _out(f"  {C.green(GLYPH_CHECK)} {w.name}")
    return 0


def cmd_hooks_uninstall(args: argparse.Namespace) -> int:
    repo = Path(args.path).resolve()
    touched = hooks.uninstall_hooks(repo)
    if not touched:
        _out(C.dim("No SkillSyncer hooks were installed in this repo."))
        return 0
    _out(_ok(f"Removed SkillSyncer block from {len(touched)} hook(s)"))
    for t in touched:
        _out(f"  {C.green(GLYPH_CHECK)} {t.name}")
    return 0


def cmd_hooks_status(args: argparse.Namespace) -> int:
    repo = Path(args.path).resolve()
    _out("")
    _out(_section(f"Hook status: {repo}"))
    for name in ("pre-push", "post-merge"):
        if hooks.hook_is_installed(repo, name):
            _out(f"  {C.green(GLYPH_CHECK)} {name:<12} installed")
        else:
            _out(f"  {C.dim(GLYPH_BULLET)} {name:<12} {C.dim('not installed')}")
    return 0


# ---------------------------------------------------------------------------
# skill (singular) — inspect a specific skill
# ---------------------------------------------------------------------------


def cmd_skill_show(args: argparse.Namespace) -> int:
    matches = [s for s in _find_local_skills() if s["name"] == args.name]
    if not matches:
        _err(f"[skillsyncer] no skill named '{args.name}' found in any agent dir.")
        return 2

    import re as _re
    placeholder_re = _re.compile(r"\$\{\{([A-Z_][A-Z0-9_]*)\}\}")
    identity = read_identity()
    secrets = identity.get("secrets") or {}

    for skill in matches:
        md = skill["md"]
        try:
            content = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""

        placeholders = sorted(set(placeholder_re.findall(content)))
        from .scanner import scan_content
        detections = scan_content(content, secrets)

        _out("")
        _out(_section(f"Skill: {skill['name']}"))
        _out(f"  {C.dim('agent:'):<14} {skill['agent']}")
        _out(f"  {C.dim('path:'):<14} {md}")
        _out(f"  {C.dim('size:'):<14} {len(content)} bytes")
        _out(f"  {C.dim('placeholders:'):<14} {len(placeholders)}")
        for p in placeholders:
            mark = C.green(GLYPH_CHECK) if p in secrets else C.yellow("!")
            status = "filled" if p in secrets else "missing"
            _out(f"      {mark} {p:<24} {C.dim(status)}")
        if detections:
            _out(f"  {C.red('hardcoded secrets:')} {len(detections)}")
            for d in detections:
                _out(f"      {C.red(GLYPH_CROSS)} line {d['line']}: {d['pattern_label']}")
        managed = "skillsyncer:require" in content
        _out(f"  {C.dim('managed:'):<14} " + (C.green("yes") if managed else C.yellow("no")))
    return 0


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


# ---------------------------------------------------------------------------
# dev — dangerous helpers for local testing
# ---------------------------------------------------------------------------


def cmd_dev_purge(args: argparse.Namespace) -> int:
    """Wipe all tracked content from a source repo and commit.

    This is a dev-only escape hatch for resetting a test repo to a
    clean state before re-running ``skillsyncer publish --all``.  It
    removes every tracked file, commits the result, and leaves the
    local clone intact so the next publish can repopulate it.

    It will NOT push — the user must push manually, as with publish.
    """
    config = read_config()
    target, err = _resolve_publish_target(config, getattr(args, "source", None))
    if err:
        _err(f"[skillsyncer] {err}")
        return 2

    target_path = Path(target.get("path") or "").expanduser()
    if not target_path.is_dir():
        _err(f"[skillsyncer] source dir doesn't exist: {target_path}")
        return 2
    if not (target_path / ".git").exists():
        _err(f"[skillsyncer] not a git repo: {target_path}")
        return 2

    # Count locally tracked files.
    ls = subprocess.run(
        ["git", "-C", str(target_path), "ls-files"],
        capture_output=True, text=True, check=False,
    )
    tracked = [f for f in ls.stdout.splitlines() if f.strip()]
    needs_local_purge = bool(tracked)

    # Resolve the upstream remote so we can name it in the warning.
    remote_proc = subprocess.run(
        ["git", "-C", str(target_path), "remote", "get-url", "origin"],
        capture_output=True, text=True, check=False,
    )
    remote_url = remote_proc.stdout.strip() if remote_proc.returncode == 0 else None

    # If there's nothing to do at all, exit early.
    if not needs_local_purge and not args.push:
        _out("[skillsyncer] nothing to purge — repo already has no tracked files.")
        return 0

    if args.push and not remote_url:
        _err("[skillsyncer] --push requested but no origin remote found. Aborting.")
        return 2

    # ── Danger warning ────────────────────────────────────────────────
    _err("")
    _err(C.yellow("  ══════════════════════════════════════════════════"))
    _err(C.yellow(f"  ⚠️   DEV PURGE — {C.bold(target['name'])}"))
    _err(C.yellow("  ══════════════════════════════════════════════════"))
    _err("")
    _err(f"  Local clone:  {target_path}")
    if remote_url:
        _err(f"  Upstream:     {remote_url}")
    if needs_local_purge:
        _err(f"  Local files:  {len(tracked)} tracked file(s) will be deleted and committed")
    else:
        _err("  Local clone:  already empty — no commit needed")
    _err("")
    if args.push:
        _err(C.red("  --push is set: the empty state will be pushed to the upstream."))
        _err(C.red("  THIS CANNOT BE UNDONE without force-pushing a recovery commit."))
    else:
        _err("  Local only — nothing is pushed unless you pass --push.")
        _err("  Recovery is possible via the remote or a local reflog.")
    _err("")

    if args.yes:
        confirm = "PURGE"
    elif sys.stdin.isatty():
        try:
            confirm = input("  Type PURGE to confirm (anything else cancels): ").strip()
        except (EOFError, KeyboardInterrupt):
            _out("\n  Cancelled.")
            return 0
    else:
        _err("  Non-interactive shell — pass --yes to skip this prompt.")
        return 2

    if confirm != "PURGE":
        _out("  Cancelled.")
        return 0

    if needs_local_purge:
        try:
            subprocess.run(
                ["git", "-C", str(target_path), "rm", "-rf", "--quiet", "."],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(target_path), "commit",
                 "-m", "Dev purge via SkillSyncer --dev purge"],
                check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            _err(f"[skillsyncer] git error: {exc}")
            return 2

    _out("")
    if needs_local_purge:
        _out(_ok(f"Purged {len(tracked)} file(s) from {C.bold(target['name'])} (local)"))
    else:
        _out(_ok(f"Local clone already empty — skipping commit"))

    if args.push:
        _out(C.dim(f"  Pushing to {remote_url}…"))
        try:
            subprocess.run(
                ["git", "-C", str(target_path), "push"],
                check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            _err(f"[skillsyncer] push failed: {exc}")
            _err("  Local purge commit is intact — fix the push manually.")
            return 2
        _out(_ok("Pushed purge commit to upstream."))

    _out(C.dim("  Repo is now empty. Run `skillsyncer publish --all` to repopulate."))
    if not args.push:
        _out(C.dim(f"  Push when ready:  git -C {target_path} push"))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillsyncer",
        description="SkillSyncer \u2014 agent skills that sync, fill, and protect themselves.",
    )
    parser.add_argument("--version", action="version", version=f"skillsyncer {__version__}")
    parser.add_argument(
        "--dev", action="store_true",
        help="Enable dev-mode commands (destructive, for local testing only).",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # init
    # onboard — interactive wizard (preferred first-run path)
    p = sub.add_parser("onboard", help="Interactive setup wizard: scan → source repo → render.")
    p.set_defaults(func=cmd_onboard)

    # init (headless / agent path)
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

    # skill (singular) — inspect a specific skill
    p_skill = sub.add_parser("skill", help="Inspect a single skill by name.")
    skill_sub = p_skill.add_subparsers(dest="skill_command", metavar="SKILL_COMMAND")
    skill_sub.required = True

    sp = skill_sub.add_parser("show", help="Show metadata, placeholders, and any hardcoded secrets for a skill.")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_skill_show)

    # sync — pull every source then render
    p = sub.add_parser("sync", help="Git pull every source repo, then render all skills.")
    p.set_defaults(func=cmd_sync)

    # doctor — diagnostics
    p = sub.add_parser("doctor", help="Diagnose SkillSyncer state, hooks, missing tools.")
    p.set_defaults(func=cmd_doctor)

    # sources group
    p_sources = sub.add_parser("sources", help="Manage registered skill source repos.")
    sources_sub = p_sources.add_subparsers(dest="sources_command", metavar="SOURCES_COMMAND")
    sources_sub.required = True

    sp = sources_sub.add_parser("list", help="List registered sources.")
    sp.set_defaults(func=cmd_sources_list)

    sp = sources_sub.add_parser("show", help="Show details about one source.")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_sources_show)

    sp = sources_sub.add_parser("remove", help="Remove a source from config.yaml (does not delete the cloned repo).")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_sources_remove)

    # hooks group
    p_hooks = sub.add_parser("hooks", help="Install / uninstall / inspect git hooks in a repo.")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_command", metavar="HOOKS_COMMAND")
    hooks_sub.required = True

    sp = hooks_sub.add_parser("install", help="Install pre-push and post-merge hooks into a git repo.")
    sp.add_argument("--path", default=".", help="Path to the git repo (default: cwd).")
    sp.set_defaults(func=cmd_hooks_install)

    sp = hooks_sub.add_parser("uninstall", help="Remove the SkillSyncer block from hooks (leaves anything else intact).")
    sp.add_argument("--path", default=".", help="Path to the git repo (default: cwd).")
    sp.set_defaults(func=cmd_hooks_uninstall)

    sp = hooks_sub.add_parser("status", help="Check whether hooks are installed in a repo.")
    sp.add_argument("--path", default=".", help="Path to the git repo (default: cwd).")
    sp.set_defaults(func=cmd_hooks_status)

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

    # ------------------------------------------------------------------
    # dev — dangerous commands for local dev/testing only.
    # Hidden from normal --help; only reachable via `skillsyncer --dev`.
    # ------------------------------------------------------------------
    p_dev = sub.add_parser(
        "dev",
        help=argparse.SUPPRESS,
        description=(
            "Dev-mode commands. Destructive. "
            "Always require explicit confirmation. "
            "Never use in production."
        ),
    )
    dev_sub = p_dev.add_subparsers(dest="dev_command", metavar="DEV_COMMAND")
    dev_sub.required = True

    dp = dev_sub.add_parser(
        "purge",
        help="Wipe all content from a registered source repo and commit.",
    )
    dp.add_argument(
        "--source", default=None,
        help="Which source repo to purge (only needed when more than one is registered).",
    )
    dp.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt (dangerous — use only in scripts).",
    )
    dp.add_argument(
        "--push", action="store_true",
        help="Also push the purge commit to the upstream remote (irreversible without force-push).",
    )
    dp.set_defaults(func=cmd_dev_purge)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    C.init()
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Gate: dev-group commands require an explicit --dev flag so they
    # can't be triggered by accident in production environments.
    if getattr(args, "dev_command", None) and not getattr(args, "dev", False):
        parser.error(
            "`skillsyncer dev` commands are development-only.\n"
            "Re-run with the --dev flag:  skillsyncer --dev dev purge"
        )

    rc = args.func(args)
    if rc:
        sys.exit(rc)
    return 0


if __name__ == "__main__":
    main()
