"""Config file (``~/.skillsyncer/config.yaml``) read/write.

The config tracks where skills come from (sources) and where
rendered output should land (targets).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import paths
from ._io import atomic_write

KNOWN_AGENTS = [
    {"name": "claude-code", "paths": ["~/.claude/skills"]},
    {"name": "cursor", "paths": ["~/.cursor/skills"]},
    {"name": "windsurf", "paths": ["~/.windsurf/skills"]},
    {"name": "gemini", "paths": ["~/.gemini/skills"]},
    {"name": "codex", "paths": ["~/.codex/skills"]},
]


def _resolve(path: str | Path | None) -> Path:
    return Path(path) if path else paths.config_path()


def read_config(path: str | Path | None = None) -> dict:
    p = _resolve(path)
    if not p.exists():
        return {"sources": [], "targets": []}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("sources", [])
    data.setdefault("targets", [])
    if data["sources"] is None:
        data["sources"] = []
    if data["targets"] is None:
        data["targets"] = []
    return data


def write_config(config: dict, path: str | Path | None = None) -> None:
    p = _resolve(path)
    payload = {
        "sources": config.get("sources") or [],
        "targets": config.get("targets") or [],
    }
    atomic_write(p, yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))


def add_source(url: str, name: str, path: str | Path | None = None) -> None:
    """Add or update a source. Matches by ``name`` first, then ``url``."""
    config = read_config(path)
    sources = config.setdefault("sources", [])
    for src in sources:
        if src.get("name") == name or src.get("url") == url:
            src["name"] = name
            src["url"] = url
            break
    else:
        sources.append({"name": name, "url": url})
    write_config(config, path)


def detect_targets() -> list[dict]:
    """Probe known agent skill directories on this machine.

    A target is included when its parent dir exists, even if the
    skills/ subdir hasn't been created yet — that means the agent
    is installed but has no skills, which is exactly when we want
    to be able to render into it.
    """
    targets: list[dict] = []
    for agent in KNOWN_AGENTS:
        for raw in agent["paths"]:
            p = Path(raw).expanduser()
            if p.exists() or p.parent.exists():
                targets.append({
                    "name": agent["name"],
                    "path": str(p),
                    "found": p.exists(),
                })
                break
    return targets
