"""Per-skill sync state tracking.

``state.yaml`` records the last hash and last-rendered timestamp for
each skill. Drift detection compares the recorded hash against the
current SKILL.md to find what's new since the last render.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from . import paths
from ._io import atomic_write


def _resolve(path: str | Path | None) -> Path:
    return Path(path) if path else paths.state_path()


def read_state(path: str | Path | None = None) -> dict:
    p = _resolve(path)
    if not p.exists():
        return {"skills": {}}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("skills", {})
    if data["skills"] is None:
        data["skills"] = {}
    return data


def write_state(state: dict, path: str | Path | None = None) -> None:
    p = _resolve(path)
    payload = {"skills": state.get("skills") or {}}
    atomic_write(p, yaml.safe_dump(payload, sort_keys=True, default_flow_style=False))


def update_skill_state(
    skill_name: str,
    path: str | Path | None = None,
    **fields,
) -> None:
    state = read_state(path)
    state.setdefault("skills", {}).setdefault(skill_name, {}).update(fields)
    write_state(state, path)


def hash_file(file_path: str | Path) -> str:
    return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()


def get_drift(
    config: dict | None = None,
    state_path: str | Path | None = None,
) -> list[dict]:
    """Return skills whose current SKILL.md hash differs from state.yaml."""
    if config is None:
        from .config import read_config
        config = read_config()

    state = read_state(state_path)
    recorded_skills = state.get("skills", {}) or {}
    drifted: list[dict] = []

    for source in config.get("sources") or []:
        repo_path = source.get("path") or source.get("local_path")
        if not repo_path:
            continue
        repo_root = Path(repo_path).expanduser()
        if not repo_root.is_dir():
            continue
        for skill_dir in sorted(repo_root.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            md = skill_dir / "SKILL.md"
            if not md.is_file():
                continue
            current = hash_file(md)
            recorded = (recorded_skills.get(skill_dir.name) or {}).get("hash")
            if recorded != current:
                drifted.append({
                    "name": skill_dir.name,
                    "source": str(repo_root),
                    "current_hash": current,
                    "recorded_hash": recorded,
                })
    return drifted
