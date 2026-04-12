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
from .discoverer import discover
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


def _out(msg: str = "") -> None:
    print(msg)


def _err(msg: str = "") -> None:
    print(msg, file=sys.stderr)


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

    _out(f"\u2713 SkillSyncer initialized at {home}\n")

    found_agents = [a for a in proposal["agents"] if a["found"]]
    if found_agents:
        _out("Agents detected:")
        for a in found_agents:
            _out(f"  \u2713 {a['name']:<14} {a['path']}")
    else:
        _out("No known agents detected on this machine.")

    if proposal["existing_skills"]:
        # Group by agent and cap each group so the output stays readable
        # even on machines with hundreds of skills.
        SKILLS_PER_AGENT = 8
        by_agent: dict[str, list[dict]] = {}
        for s in proposal["existing_skills"]:
            by_agent.setdefault(s["agent"], []).append(s)

        total = len(proposal["existing_skills"])
        _out(f"\nExisting skills: {total}")
        for agent_name in sorted(by_agent):
            group = by_agent[agent_name]
            _out(f"  {agent_name} ({len(group)}):")
            for s in group[:SKILLS_PER_AGENT]:
                tags = []
                if s["has_placeholders"]:
                    tags.append("placeholders")
                if s["has_hardcoded_secrets"]:
                    tags.append("hardcoded-secret")
                tail = f" [{', '.join(tags)}]" if tags else ""
                _out(f"    \u00b7 {s['name']}{tail}")
            if len(group) > SKILLS_PER_AGENT:
                _out(f"    \u00b7 \u2026 and {len(group) - SKILLS_PER_AGENT} more")

    if not proposal.get("credential_scan_performed"):
        _out("\nCredentials: scan skipped.")
        _out("  \u2192 re-run `skillsyncer init --yes` to scan, or set secrets")
        _out("    by hand with `skillsyncer secret-set <KEY> <VALUE>`.")
    elif proposal["credentials"]:
        _out(f"\nCredentials found: {len(proposal['credentials'])}")
        for c in proposal["credentials"]:
            # Print KEY NAMES only — never values.
            _out(f"  \u00b7 {c['key']:<24} from {c['source']}")
        _out("\n  \u2192 re-run with `skillsyncer secret-set <KEY> <VALUE>` to import.")
    else:
        _out("\nCredentials: scan completed, nothing matched.")

    git = proposal["git"]
    if git["current_project_remote"]:
        _out(f"\nCurrent project remote: {git['current_project_remote']}")
    if git["gh_authenticated"]:
        _out("gh CLI authenticated \u2014 `skillsyncer add` can clone private repos.")

    _out("\nNext: skillsyncer add <git-url>  to register a skills source.")
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
    _out(f"[skillsyncer] added source: {name}")
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
        _err("[skillsyncer] some skills still need credentials:")
        for skill, keys in fill_report["unfilled"].items():
            _err(f"  {skill}: {', '.join(keys)}")
        return 1
    _out(f"[skillsyncer] rendered {len(fill_report['skills'])} skill(s)")
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
    parser = _build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    if rc:
        sys.exit(rc)
    return 0


if __name__ == "__main__":
    main()
