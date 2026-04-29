"""Template engine for ``${{KEY}}`` placeholders.

The engine is intentionally tiny: a single regex, a single-pass
substitution, and a deterministic resolution order.

Rendering walks the entire skill directory: ``SKILL.md`` and any other
text file is hydrated through the placeholder engine, while binary
assets (png/jpg/pdf/...) are copied byte-for-byte. Subdirectories like
``assets/``, ``templates/``, ``scripts/``, ``references/`` are
preserved. Junk paths (``.git``, ``node_modules``, caches) are skipped.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from ._io import atomic_copy, atomic_write

PLACEHOLDER_RE = re.compile(r"\$\{\{([A-Z_][A-Z0-9_]*)\}\}")

# File extensions we treat as text and feed through the placeholder
# engine. Anything else is copied byte-for-byte. Kept lowercase.
_TEXT_EXTENSIONS = frozenset({
    ".md", ".markdown", ".mdx", ".rst", ".txt", ".text",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf",
    ".env",
    ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".rb", ".go", ".rs", ".java", ".kt", ".swift", ".php",
    ".c", ".cpp", ".h", ".hpp", ".html", ".htm", ".css", ".scss", ".sass",
    ".sql", ".graphql", ".gql", ".lua", ".pl", ".r",
    ".tf", ".tfvars", ".hcl",
})

# Files (no extension or unusual names) we still want to render as text.
_TEXT_FILENAMES = frozenset({
    ".gitignore", ".gitattributes", ".editorconfig",
    "dockerfile", "makefile", "readme",
})

# Directory and file names we never copy out of a source skill: VCS
# metadata, build artifacts, vendored deps, editor caches, OS junk.
_SKIP_DIR_NAMES = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components", "vendor",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".venv", "venv", "env",
    "dist", "build", ".next", ".cache",
    ".idea", ".vscode",
})
_SKIP_FILE_NAMES = frozenset({
    ".DS_Store", "Thumbs.db", "desktop.ini",
})
# File suffixes always copied as binary even if a tool happened to add
# them to _TEXT_EXTENSIONS by mistake.
_BINARY_HINTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff",
    ".svg",  # SVGs are text but commonly authored as binary assets;
             # we copy verbatim to avoid mangling tool-generated XML.
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".m4a", ".wav", ".ogg", ".webm", ".mov",
    ".pptx", ".docx", ".xlsx", ".odt", ".odp", ".ods",
    ".pyc", ".so", ".dll", ".dylib", ".o", ".a",
    ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
})


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


def _is_text_file(path: Path) -> bool:
    """Treat the file as text iff its suffix is a known text extension
    (and not in the binary-hint set), or its name is in the
    text-filenames whitelist."""
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix in _BINARY_HINTS:
        return False
    if suffix in _TEXT_EXTENSIONS:
        return True
    return name in _TEXT_FILENAMES


def _iter_skill_files(skill_dir: Path) -> Iterable[Path]:
    """Yield every file under ``skill_dir`` that should be rendered or
    copied, skipping VCS / cache / OS-junk paths and hidden subdirs."""
    for child in sorted(skill_dir.iterdir()):
        name = child.name
        if name in _SKIP_FILE_NAMES:
            continue
        if child.is_dir():
            if name in _SKIP_DIR_NAMES or name.startswith("."):
                continue
            yield from _iter_skill_files(child)
        elif child.is_file():
            yield child


def _prepare_skill_payload(
    skill_dir: Path,
    manifest: dict | None,
    identity: dict | None,
) -> tuple[list[tuple[Path, str, bytes | None]], list[str]]:
    """Walk ``skill_dir`` and produce a list of write instructions.

    Each instruction is ``(relative_path, kind, payload)`` where
    ``kind`` is ``"text"`` or ``"binary"``. For text entries, ``payload``
    is the rendered UTF-8 string encoded as bytes carrier (None when
    text); the actual rendered text is stored alongside as the
    ``payload`` byte string for symmetry.

    Returns ``(items, unfilled_keys)``. ``unfilled_keys`` is dedup'd in
    first-appearance order across every text file in the skill.
    """
    items: list[tuple[Path, str, bytes | None]] = []
    unfilled: list[str] = []
    seen: set[str] = set()

    for src in _iter_skill_files(skill_dir):
        rel = src.relative_to(skill_dir)
        if _is_text_file(src):
            try:
                content = src.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # Mis-typed extension or unreadable: copy as binary so
                # we never lose the file.
                items.append((rel, "binary", None))
                continue
            rendered, file_unfilled = render_skill(content, manifest, identity)
            for k in file_unfilled:
                if k not in seen:
                    seen.add(k)
                    unfilled.append(k)
            items.append((rel, "text", rendered.encode("utf-8")))
        else:
            items.append((rel, "binary", None))

    return items, unfilled


def render_skill_dir(
    skill_dir: Path,
    target_dir: Path,
    manifest: dict | None,
    identity: dict | None,
) -> tuple[list[str], list[str]]:
    """Render and copy a single skill directory into ``target_dir``.

    Convenience wrapper around ``_prepare_skill_payload`` used by
    callers that only have one target.
    """
    items, unfilled = _prepare_skill_payload(skill_dir, manifest, identity)
    written: list[str] = []
    for rel, kind, payload in items:
        dst = target_dir / rel
        if kind == "text":
            atomic_write(dst, payload.decode("utf-8"))
        else:
            atomic_copy(skill_dir / rel, dst)
        written.append(str(dst))
    return written, unfilled


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

            items, unfilled = _prepare_skill_payload(skill_dir, manifest, identity)

            for target in targets:
                tdir = Path(target["path"]).expanduser() / skill_dir.name
                tdir.mkdir(parents=True, exist_ok=True)
                for rel, kind, payload in items:
                    dst = tdir / rel
                    if kind == "text":
                        atomic_write(dst, payload.decode("utf-8"))
                    else:
                        atomic_copy(skill_dir / rel, dst)
                    report["written"].append(str(dst))

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
