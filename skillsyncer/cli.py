"""SkillSyncer command-line interface.

The CLI is a thin layer over the modules: every command reads/writes
``identity.yaml``, ``config.yaml``, and ``state.yaml`` and shells the
work out. Exit codes are load-bearing — git hooks depend on them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click
import yaml

from . import __version__, hooks, paths
from .config import KNOWN_AGENTS, add_source, detect_targets, read_config, write_config
from .discoverer import discover
from .filler import auto_fill
from .guarder import guard_fix
from .identity import list_secret_keys, read_identity, set_secret, write_identity
from .renderer import render_all_skills
from .reporter import (
    create_report,
    finalize_report,
    latest_report,
    list_reports,
    read_report,
    update_report,
)
from .scanner import scan_content, scan_file, scan_staged_files
from .state import get_drift, hash_file, read_state, update_skill_state, write_state

_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
}
_TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".yaml", ".yml", ".json", ".toml", ".ini",
    ".env", ".sh", ".py", ".js", ".ts", ".rb", ".go", ".rs", ".java",
    ".kt", ".swift", ".php", ".c", ".cpp", ".h", ".hpp", ".html", ".css",
}


@click.group()
@click.version_option(__version__, prog_name="skillsyncer")
def main() -> None:
    """SkillSyncer — agent skills that sync, fill, and protect themselves."""


# ---------------------------------------------------------------------------
# init / add
# ---------------------------------------------------------------------------


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Print discovery result as JSON (no writes)")
def init(as_json: bool) -> None:
    """One-time setup: scan environment, write config, surface a proposal."""
    home = paths.home()
    home.mkdir(parents=True, exist_ok=True)

    proposal = discover()

    if as_json:
        # Strip credential VALUES — only key names are safe to print.
        safe = dict(proposal)
        safe["credentials"] = [
            {"key": c["key"], "source": c["source"], "path": c["path"]}
            for c in proposal["credentials"]
        ]
        click.echo(json.dumps(safe, indent=2))
        return

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

    click.echo(f"✓ SkillSyncer initialized at {home}\n")

    found_agents = [a for a in proposal["agents"] if a["found"]]
    if found_agents:
        click.echo("Agents detected:")
        for a in found_agents:
            click.echo(f"  ✓ {a['name']:<14} {a['path']}")
    else:
        click.echo("No known agents detected on this machine.")

    if proposal["existing_skills"]:
        click.echo(f"\nExisting skills: {len(proposal['existing_skills'])}")
        for s in proposal["existing_skills"]:
            tags = []
            if s["has_placeholders"]:
                tags.append("placeholders")
            if s["has_hardcoded_secrets"]:
                tags.append("hardcoded-secret")
            tail = f" [{', '.join(tags)}]" if tags else ""
            click.echo(f"  · {s['agent']}/{s['name']}{tail}")

    if proposal["credentials"]:
        click.echo(f"\nCredentials found: {len(proposal['credentials'])}")
        for c in proposal["credentials"]:
            # Print KEY NAMES only — never values.
            click.echo(f"  · {c['key']:<24} from {c['source']}")
        click.echo("\n  → re-run with `skillsyncer secret-set <KEY> <VALUE>` to import.")

    git = proposal["git"]
    if git["current_project_remote"]:
        click.echo(f"\nCurrent project remote: {git['current_project_remote']}")
    if git["gh_authenticated"]:
        click.echo("gh CLI authenticated — `skillsyncer add` can clone private repos.")

    click.echo("\nNext: skillsyncer add <git-url>  to register a skills source.")


@main.command()
@click.argument("url")
@click.option("--name", default=None, help="Alias for this source")
@click.option("--no-clone", is_flag=True, help="Skip git clone (for local paths)")
def add(url: str, name: str | None, no_clone: bool) -> None:
    """Add a skill source repo and install git hooks."""
    if not name:
        name = url.rstrip("/").split("/")[-1].removesuffix(".git") or "source"

    target = paths.repos_dir() / name
    target.parent.mkdir(parents=True, exist_ok=True)

    if no_clone:
        if not Path(url).is_dir():
            raise click.ClickException(f"--no-clone requires an existing dir: {url}")
        local_path = str(Path(url).resolve())
    else:
        if target.exists():
            click.echo(f"[skillsyncer] {name} already cloned, pulling…")
            subprocess.run(["git", "-C", str(target), "pull", "--ff-only"], check=False)
        else:
            click.echo(f"[skillsyncer] cloning {url} → {target}")
            subprocess.run(["git", "clone", url, str(target)], check=True)
        local_path = str(target)
        try:
            hooks.install_hooks(local_path)
            click.echo("[skillsyncer] hooks installed")
        except FileNotFoundError as exc:
            click.echo(f"[skillsyncer] hook install skipped: {exc}", err=True)

    config = read_config()
    sources = config.setdefault("sources", [])
    for src in sources:
        if src.get("name") == name:
            src.update({"name": name, "url": url, "path": local_path})
            break
    else:
        sources.append({"name": name, "url": url, "path": local_path})
    write_config(config)
    click.echo(f"[skillsyncer] added source: {name}")


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


@main.command()
@click.option("--report", "report_path", default=None, help="Report file path")
def render(report_path: str | None) -> None:
    """Hydrate ${{}} placeholders and write to agent target dirs."""
    config = read_config()
    identity = read_identity()
    fill_report = render_all_skills(config, identity)

    # Update state.yaml hashes for what we just rendered.
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

    if report_path:
        report = read_report(report_path) if Path(report_path).exists() else create_report("fill", path=report_path)
        update_report(report, {"phase": "render", "result": fill_report})
        finalize_report(report, status="partial" if fill_report["unfilled"] else "passed")

    if fill_report["unfilled"]:
        click.echo("[skillsyncer] some skills still need credentials:", err=True)
        for skill, keys in fill_report["unfilled"].items():
            click.echo(f"  {skill}: {', '.join(keys)}", err=True)
        sys.exit(1)
    click.echo(f"[skillsyncer] rendered {len(fill_report['skills'])} skill(s)")


@main.command(name="fill")
@click.option("--auto", "auto_fill_flag", is_flag=True, help="Auto-fill from env/cascade")
@click.option("--report", "report_path", default=None)
def fill_cmd(auto_fill_flag: bool, report_path: str | None) -> None:
    """Resolve unfilled placeholders from env, identity, cascade."""
    config = read_config()
    identity = read_identity()
    skills = _iter_skills(config)

    if not auto_fill_flag:
        click.echo("[skillsyncer] interactive fill not yet implemented; use --auto", err=True)
        sys.exit(2)

    found, missing = auto_fill(skills, identity)
    for key, value in found.items():
        set_secret(key, value)

    if report_path:
        report = read_report(report_path) if Path(report_path).exists() else create_report("fill", path=report_path)
        update_report(report, {
            "phase": "fill",
            "newly_found_keys": sorted(found.keys()),
            "still_missing": missing,
        })
        # Don't finalize — render will.
        from ._io import atomic_write
        atomic_write(Path(report_path), json.dumps(report, indent=2))

    if found:
        click.echo(f"[skillsyncer] resolved {len(found)} key(s): {', '.join(sorted(found.keys()))}")
        sys.exit(0)
    click.echo("[skillsyncer] no new values resolved", err=True)
    sys.exit(1)


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


@main.command()
@click.option("--staged", is_flag=True, help="Only scan staged files")
@click.option("--format", "fmt", default="human", type=click.Choice(["human", "json"]))
@click.option("--path", "scan_path", default=".", help="Path to scan when not --staged")
def scan(staged: bool, fmt: str, scan_path: str) -> None:
    """Detect potential secrets in files (regex, no AI)."""
    identity = read_identity()
    secrets = identity.get("secrets") or {}

    if staged:
        try:
            detections = scan_staged_files(scan_path, identity)
        except subprocess.CalledProcessError as exc:
            click.echo(f"[skillsyncer] git error: {exc}", err=True)
            sys.exit(2)
    else:
        detections = []
        root = Path(scan_path)
        if root.is_file():
            detections = scan_file(root, secrets)
        else:
            for f in _walk_text_files(root):
                detections.extend(scan_file(f, secrets))

    if fmt == "json":
        click.echo(json.dumps({"detections": detections}, indent=2))
    else:
        if not detections:
            click.echo("✓ No secrets detected")
        else:
            click.echo(f"Found {len(detections)} potential secret(s):")
            for d in detections:
                loc = d.get("file", "?")
                click.echo(f"  {loc}:{d['line']}: {d['pattern_label']} — {d['matched_text']}")

    sys.exit(1 if detections else 0)


@main.command()
@click.option("--fix", is_flag=True, help="Auto-replace detected secrets")
@click.option("--report", "report_path", default=None)
@click.option("--path", "repo_path", default=".", help="Repo to operate on")
def guard(fix: bool, report_path: str | None, repo_path: str) -> None:
    """Scan staged files and optionally auto-fix secrets."""
    identity = read_identity()
    try:
        detections = scan_staged_files(repo_path, identity)
    except subprocess.CalledProcessError as exc:
        click.echo(f"[skillsyncer] git error: {exc}", err=True)
        sys.exit(2)

    fixes: list[dict] = []
    if fix:
        fixes = guard_fix(repo_path, identity, detections)

    if report_path:
        report = read_report(report_path) if Path(report_path).exists() else create_report("guard", path=report_path)
        update_report(report, {"phase": "guard", "detections": detections, "fixes": fixes})
        from ._io import atomic_write
        atomic_write(Path(report_path), json.dumps(report, indent=2))

    if not detections:
        click.echo("✓ No secrets detected")
        sys.exit(0)

    click.echo(f"[skillsyncer] {len(detections)} detection(s)")
    for d in detections:
        click.echo(f"  {d.get('file', '?')}:{d['line']}: {d['pattern_label']}")

    if fix:
        fixed = sum(1 for f in fixes if f["status"] == "fixed")
        unresolved = sum(1 for f in fixes if f["status"] == "unresolved")
        click.echo(f"[skillsyncer] fixed={fixed} unresolved={unresolved}")
        sys.exit(0 if unresolved == 0 else 1)
    sys.exit(1)


# ---------------------------------------------------------------------------
# diff / status / secrets
# ---------------------------------------------------------------------------


@main.command(name="diff-since-last-sync")
def diff_since_last_sync() -> None:
    """Print skills that changed since the last render."""
    drift = get_drift()
    for d in drift:
        click.echo(d["name"])


@main.command(name="secret-set")
@click.argument("key")
@click.argument("value")
def secret_set_cmd(key: str, value: str) -> None:
    """Add or update a secret in identity.yaml."""
    set_secret(key, value)
    click.echo(f"Set {key}")


@main.command(name="secret-list")
def secret_list_cmd() -> None:
    """Show secret key names (not values)."""
    keys = list_secret_keys()
    if not keys:
        click.echo("(no secrets)")
    for key in keys:
        click.echo(f"  {key}")


@main.command()
def status() -> None:
    """Show skills, versions, and missing secrets."""
    config = read_config()
    identity = read_identity()
    state = read_state()
    skills = _iter_skills(config)

    click.echo(f"home:    {paths.home()}")
    click.echo(f"sources: {len(config.get('sources') or [])}")
    click.echo(f"targets: {len(config.get('targets') or [])}")
    click.echo(f"secrets: {len(identity.get('secrets') or {})} stored")
    click.echo("")

    if not skills:
        click.echo("(no skills found)")
        return

    _, missing = auto_fill(skills, identity, env={})
    click.echo("skills:")
    for name in sorted(skills):
        skill_state = (state.get("skills") or {}).get(name) or {}
        recorded = skill_state.get("hash", "—")[:8] if skill_state.get("hash") else "—"
        miss = missing.get(name) or []
        marker = "synced" if not miss else f"missing {len(miss)}"
        click.echo(f"  {name:<28} {recorded:<10} {marker}")
        for m in miss:
            click.echo(f"      need {m['key']}{(' — ' + m['description']) if m['description'] else ''}")


# ---------------------------------------------------------------------------
# report subgroup
# ---------------------------------------------------------------------------


@main.group()
def report() -> None:
    """Manage guard and fill reports."""


@report.command(name="create")
@click.option("--type", "rtype", required=True, type=click.Choice(["fill", "guard"]))
@click.option("--path", "report_path", default=None)
def report_create(rtype: str, report_path: str | None) -> None:
    r = create_report(rtype, path=report_path)
    click.echo(r["path"])


@report.command(name="update")
@click.argument("report_path")
@click.option("--attempt", default=None)
@click.option("--issues", default=None)
def report_update(report_path: str, attempt: str | None, issues: str | None) -> None:
    r = read_report(report_path)
    update_report(r, {"attempt": attempt, "issues": issues})


@report.command(name="finalize")
@click.argument("report_path")
@click.option("--status", "rstatus", required=True, type=click.Choice(["passed", "failed", "partial"]))
def report_finalize(report_path: str, rstatus: str) -> None:
    r = read_report(report_path)
    finalize_report(r, status=rstatus)


@report.command(name="status")
@click.argument("report_path")
def report_status(report_path: str) -> None:
    r = read_report(report_path)
    click.echo(r.get("final_status") or "in-progress")


@report.command(name="latest")
@click.option("--type", "rtype", default=None, type=click.Choice(["fill", "guard"]))
def report_latest(rtype: str | None) -> None:
    r = latest_report(rtype)
    if r is None:
        click.echo("(no reports)")
        return
    click.echo(json.dumps(r, indent=2))


@report.command(name="list")
def report_list_cmd() -> None:
    files = list_reports()
    if not files:
        click.echo("(no reports)")
        return
    for f in files:
        click.echo(str(f))


@report.command(name="clean")
@click.option("--days", default=30, help="Delete reports older than N days")
def report_clean(days: int) -> None:
    from .reporter import clean_old_reports
    n = clean_old_reports(days=days)
    click.echo(f"removed {n} report(s)")


if __name__ == "__main__":
    main()
