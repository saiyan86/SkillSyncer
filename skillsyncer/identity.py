"""Identity file (``~/.skillsyncer/identity.yaml``) read/write.

Layout::

    secrets:
      GATEWAY_URL: https://...
      GATEWAY_KEY: sk-...

    overrides:
      energy-diagnose:
        alarm_threshold: 0.95

This file is the source of truth for placeholder values. It must
never be committed to a git repo.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import paths
from ._io import atomic_write


def _resolve(path: str | Path | None) -> Path:
    return Path(path) if path else paths.identity_path()


def read_identity(path: str | Path | None = None) -> dict:
    p = _resolve(path)
    if not p.exists():
        return {"secrets": {}, "overrides": {}}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("secrets", {})
    data.setdefault("overrides", {})
    if data["secrets"] is None:
        data["secrets"] = {}
    if data["overrides"] is None:
        data["overrides"] = {}
    return data


def write_identity(identity: dict, path: str | Path | None = None) -> None:
    p = _resolve(path)
    payload = {
        "secrets": identity.get("secrets") or {},
        "overrides": identity.get("overrides") or {},
    }
    atomic_write(p, yaml.safe_dump(payload, sort_keys=True, default_flow_style=False))


def set_secret(key: str, value: str, path: str | Path | None = None) -> None:
    identity = read_identity(path)
    identity.setdefault("secrets", {})[key] = value
    write_identity(identity, path)


def list_secret_keys(path: str | Path | None = None) -> list[str]:
    identity = read_identity(path)
    return sorted(identity.get("secrets", {}).keys())
