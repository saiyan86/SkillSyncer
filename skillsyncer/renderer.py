"""Template engine for ``${{KEY}}`` placeholders.

The engine is intentionally tiny: a single regex, a single-pass
substitution, and a deterministic resolution order.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from ._io import atomic_write

PLACEHOLDER_RE = re.compile(r"\$\{\{([A-Z_][A-Z0-9_]*)\}\}")


def render_skill(
    skill_md: str,
    manifest: dict | None,
    identity: dict | None,
) -> tuple[str, list[str]]:
    """Render ``skill_md``, substituting placeholders.

    Resolution order per ``${{KEY}}``:
        1. ``identity["overrides"][skill_name][KEY]``
        2. ``identity["secrets"][KEY]``
        3. ``manifest["values"][KEY]``
        4. Unresolved — left as ``${{KEY}}``.

    Returns ``(rendered_text, unfilled_keys)``. ``unfilled_keys`` is
    deduplicated and preserves first-appearance order.
    """
    manifest = manifest or {}
    identity = identity or {}
    skill_name = manifest.get("name")

    overrides_root = identity.get("overrides") or {}
    overrides = overrides_root.get(skill_name, {}) if skill_name else {}
    secrets = identity.get("secrets") or {}
    values = manifest.get("values") or {}

    unfilled: list[str] = []
    seen: set[str] = set()

    def _replace(match: re.Match) -> str:
        key = match.group(1)
        if key in overrides:
            return str(overrides[key])
        if key in secrets:
            return str(secrets[key])
        if key in values:
            return str(values[key])
        if key not in seen:
            seen.add(key)
            unfilled.append(key)
        return match.group(0)

    rendered = PLACEHOLDER_RE.sub(_replace, skill_md)
    return rendered, unfilled


def render_all_skills(config: dict, identity: dict) -> dict:
    """Render every skill from every source into every target dir.

    Returns a fill report:
        {
            "skills": [{"name": str, "source": str, "unfilled": [str]}],
            "unfilled": {skill_name: [keys]},
            "written": [str],  # absolute target paths
        }

    IMPORTANT: never writes into a git-tracked source repo. Output
    only goes to the agent target directories declared in ``config``.
    """
    import yaml  # local import keeps pyyaml optional for renderer-only callers

    sources = config.get("sources") or []
    targets = config.get("targets") or []

    report: dict = {"skills": [], "unfilled": {}, "written": []}

    for source in sources:
        repo_path = source.get("path") or source.get("local_path")
        if not repo_path:
            continue
        repo_root = Path(repo_path).expanduser()
        if not repo_root.is_dir():
            continue

        for skill_dir in _iter_skill_dirs(repo_root):
            skill_md_path = skill_dir / "SKILL.md"
            if not skill_md_path.is_file():
                continue

            manifest: dict = {}
            manifest_path = skill_dir / "manifest.yaml"
            if manifest_path.is_file():
                manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}

            content = skill_md_path.read_text(encoding="utf-8")
            rendered, unfilled = render_skill(content, manifest, identity)

            for target in targets:
                tdir = Path(target["path"]).expanduser() / skill_dir.name
                tdir.mkdir(parents=True, exist_ok=True)
                out_path = tdir / "SKILL.md"
                atomic_write(out_path, rendered)
                report["written"].append(str(out_path))

            entry = {
                "name": manifest.get("name") or skill_dir.name,
                "source": str(repo_root),
                "unfilled": unfilled,
            }
            report["skills"].append(entry)
            if unfilled:
                report["unfilled"][entry["name"]] = unfilled

    return report


def _iter_skill_dirs(repo_root: Path) -> Iterable[Path]:
    """Yield candidate skill directories under a source repo."""
    for child in sorted(repo_root.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            yield child
