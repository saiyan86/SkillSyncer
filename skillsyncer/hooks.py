"""Idempotent install/uninstall of git hooks.

The hook scripts ship as templates under ``skillsyncer/templates/``
and live inside a marker block so we can replace them on upgrade
without clobbering anything the user added themselves.
"""

from __future__ import annotations

import os
from pathlib import Path

from ._io import atomic_write

START_MARKER = "# [skillsyncer:hook]"
END_MARKER = "# [/skillsyncer:hook]"

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
HOOK_FILES = {
    "pre-push": _TEMPLATES_DIR / "pre-push.sh",
    "post-merge": _TEMPLATES_DIR / "post-merge.sh",
}


def _hooks_dir(repo_path: str | Path) -> Path:
    return Path(repo_path) / ".git" / "hooks"


def _read_template(name: str) -> str:
    return HOOK_FILES[name].read_text(encoding="utf-8")


def _strip_existing_block(text: str) -> str:
    if START_MARKER not in text:
        return text
    out_lines: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        if not skipping and line.strip().startswith(START_MARKER):
            skipping = True
            continue
        if skipping and line.strip().startswith(END_MARKER):
            skipping = False
            continue
        if not skipping:
            out_lines.append(line)
    return "".join(out_lines)


def _compose(existing: str, template: str) -> str:
    """Insert the template's marker block into ``existing``.

    If existing has any prior block, it's removed first. Anything the
    user added outside the marker block is preserved at the top; an
    existing-but-empty hook (just a shebang) is treated as empty.
    """
    stripped = _strip_existing_block(existing).rstrip()
    meaningful = "\n".join(
        line for line in stripped.splitlines()
        if line.strip() and not line.strip().startswith("#!")
    ).strip()
    if not meaningful:
        return template if template.endswith("\n") else template + "\n"
    if not stripped.startswith("#!"):
        stripped = "#!/bin/bash\n" + stripped
    body_lines = template.splitlines(keepends=True)
    if body_lines and body_lines[0].startswith("#!"):
        body_lines = body_lines[1:]
    body = "".join(body_lines)
    return stripped + "\n\n" + body


def install_hooks(repo_path: str | Path) -> list[Path]:
    """Install (or refresh) skillsyncer hooks. Returns the paths written."""
    hooks_dir = _hooks_dir(repo_path)
    if not hooks_dir.exists():
        raise FileNotFoundError(f"{hooks_dir} does not exist — is this a git repo?")
    written: list[Path] = []
    for name in HOOK_FILES:
        target = hooks_dir / name
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        composed = _compose(existing, _read_template(name))
        atomic_write(target, composed)
        os.chmod(target, 0o755)
        written.append(target)
    return written


def uninstall_hooks(repo_path: str | Path) -> list[Path]:
    """Remove skillsyncer marker blocks from installed hooks."""
    hooks_dir = _hooks_dir(repo_path)
    touched: list[Path] = []
    if not hooks_dir.exists():
        return touched
    for name in HOOK_FILES:
        target = hooks_dir / name
        if not target.exists():
            continue
        existing = target.read_text(encoding="utf-8")
        stripped = _strip_existing_block(existing)
        if stripped == existing:
            continue
        # If the file is now effectively empty (just shebang), remove it.
        meaningful = "\n".join(
            line for line in stripped.splitlines() if line.strip() and not line.strip().startswith("#!")
        )
        if not meaningful.strip():
            target.unlink()
        else:
            atomic_write(target, stripped)
            os.chmod(target, 0o755)
        touched.append(target)
    return touched


def hook_is_installed(repo_path: str | Path, name: str = "pre-push") -> bool:
    target = _hooks_dir(repo_path) / name
    if not target.exists():
        return False
    return START_MARKER in target.read_text(encoding="utf-8")
