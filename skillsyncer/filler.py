"""Auto-fill placeholder values from environment, identity, and cascading sources.

The filler is the deterministic side of "fill mode": it never asks
the user, it never invents values. It just merges what's already on
the machine into a single newly_found dict.
"""

from __future__ import annotations

import os


def _required_keys(manifest: dict) -> list[tuple[str, str]]:
    """Return [(key, description)] for ``requires.secrets`` entries.

    Accepts both string and dict forms.
    """
    requires = manifest.get("requires") or {}
    out: list[tuple[str, str]] = []
    for entry in requires.get("secrets") or []:
        if isinstance(entry, dict):
            key = entry.get("name")
            if not key:
                continue
            out.append((key, entry.get("description", "")))
        else:
            out.append((str(entry), ""))
    return out


def auto_fill(
    skills: dict,
    identity: dict | None,
    env: dict | None = None,
) -> tuple[dict, dict]:
    """Try to resolve unfilled placeholders from available sources.

    Resolution order per ``${{KEY}}``:
        1. ``identity.secrets[KEY]`` — already have it.
        2. ``os.environ[KEY]`` — set in shell.
        3. Another skill already resolved this KEY (cascading fill).
        4. ``manifest.values[KEY]`` — non-secret default.

    Returns ``(newly_found, still_missing)``:
        newly_found: {KEY: value}
        still_missing: {skill_name: [{key, description, checked}]}
    """
    env = os.environ if env is None else env
    have: dict = dict((identity or {}).get("secrets") or {})
    newly_found: dict = {}

    # A few passes lets cascade-from-defaults compose if a later skill
    # exposes a value that an earlier one needs.
    for _ in range(3):
        added = False
        for manifest in skills.values():
            values = manifest.get("values") or {}
            for key, _desc in _required_keys(manifest):
                if key in have or key in newly_found:
                    continue
                if key in env:
                    newly_found[key] = env[key]
                    added = True
                    continue
                if key in values:
                    newly_found[key] = str(values[key])
                    added = True
        if not added:
            break

    still_missing: dict = {}
    for skill_name, manifest in skills.items():
        missing = []
        for key, desc in _required_keys(manifest):
            if key in have or key in newly_found:
                continue
            missing.append({
                "key": key,
                "description": desc,
                "checked": ["identity", "env", "cascade", "values"],
            })
        if missing:
            still_missing[skill_name] = missing

    return newly_found, still_missing
