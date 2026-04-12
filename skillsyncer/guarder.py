"""Auto-fix detected secrets by replacing them with ``${{KEY}}`` placeholders.

The guarder is only called *after* the scanner has produced detections.
For each detection it knows about an identity key, it rewrites the file
to use the placeholder form and ensures the skill manifest declares
the requirement. Detections without an identity key are reported as
unresolved — the operator skill will name them later.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import yaml

from ._io import atomic_write


def _placeholder(key: str) -> str:
    return "${{" + key + "}}"


def _update_manifest(skill_md_path: Path, new_keys: list[str]) -> None:
    """Add ``new_keys`` to ``manifest.yaml`` ``requires.secrets`` if present."""
    if not new_keys:
        return
    manifest_path = skill_md_path.parent / "manifest.yaml"
    if not manifest_path.is_file():
        return

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    requires = manifest.setdefault("requires", {}) or {}
    if not isinstance(requires, dict):
        requires = {}
        manifest["requires"] = requires
    secrets_list = requires.get("secrets") or []
    existing: set[str] = set()
    for entry in secrets_list:
        if isinstance(entry, dict):
            name = entry.get("name")
            if name:
                existing.add(name)
        else:
            existing.add(str(entry))

    changed = False
    for key in new_keys:
        if key in existing:
            continue
        secrets_list.append({"name": key, "description": ""})
        existing.add(key)
        changed = True

    if changed:
        requires["secrets"] = secrets_list
        manifest["requires"] = requires
        atomic_write(
            manifest_path,
            yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False),
        )


def guard_fix(
    repo_path: str | Path,
    identity: dict,
    detections: list[dict],
) -> list[dict]:
    """Replace detected secrets with placeholders. Returns a list of fixes."""
    secrets = (identity or {}).get("secrets") or {}
    fixes: list[dict] = []

    by_file: dict[str, list[dict]] = defaultdict(list)
    for det in detections:
        f = det.get("file")
        if f:
            by_file[f].append(det)

    for file_path, dets in by_file.items():
        path = Path(file_path)
        try:
            original = path.read_text(encoding="utf-8")
        except OSError:
            for det in dets:
                fixes.append(_unresolved_fix(file_path, det))
            continue

        modified = original
        added_keys: list[str] = []

        for det in dets:
            key = det.get("identity_key")
            if not key:
                fixes.append(_unresolved_fix(file_path, det))
                continue
            value = secrets.get(key)
            if not value:
                # Identity claims this key but value is missing — can't rewrite.
                fixes.append(_unresolved_fix(file_path, det, identity_key=key))
                continue
            placeholder = _placeholder(key)
            if value in modified:
                modified = modified.replace(value, placeholder)
            fixes.append({
                "file": file_path,
                "line": det["line"],
                "original": det.get("matched_text", ""),
                "replacement": placeholder,
                "status": "fixed",
                "identity_key": key,
            })
            if key not in added_keys:
                added_keys.append(key)

        if modified != original:
            atomic_write(path, modified)
            if path.name == "SKILL.md":
                _update_manifest(path, added_keys)

    return fixes


def _unresolved_fix(file_path: str, det: dict, identity_key: str | None = None) -> dict:
    return {
        "file": file_path,
        "line": det["line"],
        "original": det.get("matched_text", ""),
        "replacement": None,
        "status": "unresolved",
        "identity_key": identity_key if identity_key is not None else det.get("identity_key"),
    }
