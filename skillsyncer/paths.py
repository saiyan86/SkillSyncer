"""Resolve SkillSyncer's home directory.

Honors ``$SKILLSYNCER_HOME`` so tests (and curious users) can point
the whole tree at a temporary location without monkey-patching.
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    raw = os.environ.get("SKILLSYNCER_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".skillsyncer"


def identity_path() -> Path:
    return home() / "identity.yaml"


def config_path() -> Path:
    return home() / "config.yaml"


def state_path() -> Path:
    return home() / "state.yaml"


def reports_dir() -> Path:
    return home() / "reports"


def repos_dir() -> Path:
    return home() / "repos"
