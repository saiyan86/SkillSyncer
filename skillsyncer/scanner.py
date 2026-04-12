"""Regex-based secret detection.

Two-layer matching:
- Built-in BLOCK regex patterns (api keys, bearer tokens, ...).
- Literal cross-reference against values in identity.secrets.

Allowed regions (e.g. ``${{KEY}}`` placeholders) are excluded so
that legitimate templates never trip the scanner.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable

from .patterns import ALLOW_PATTERNS, BLOCK_PATTERNS

_DEFAULT_BLOCK = [(re.compile(p["pattern"]), p["label"]) for p in BLOCK_PATTERNS]
_DEFAULT_ALLOW = [re.compile(p) for p in ALLOW_PATTERNS]

_TRUNCATE_LEN = 24


def _truncate(s: str) -> str:
    return s if len(s) <= _TRUNCATE_LEN else s[:_TRUNCATE_LEN] + "..."


def _allowed_spans(line: str, allow_patterns: Iterable[re.Pattern]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pat in allow_patterns:
        for m in pat.finditer(line):
            spans.append((m.start(), m.end()))
    return spans


def _overlaps(a_start: int, a_end: int, spans: Iterable[tuple[int, int]]) -> bool:
    for s, e in spans:
        if a_start < e and a_end > s:
            return True
    return False


def scan_content(
    content: str,
    identity_secrets: dict | None = None,
    extra_block: list[dict] | None = None,
    extra_allow: list[str] | None = None,
) -> list[dict]:
    """Scan content; return a list of detection dicts sorted by line, column."""
    block = list(_DEFAULT_BLOCK)
    if extra_block:
        block.extend((re.compile(p["pattern"]), p["label"]) for p in extra_block)

    allow = list(_DEFAULT_ALLOW)
    if extra_allow:
        allow.extend(re.compile(p) for p in extra_allow)

    identity_secrets = identity_secrets or {}

    detections: list[dict] = []
    lines = content.splitlines()
    if not lines:
        return detections

    for lineno, line in enumerate(lines, start=1):
        allowed = _allowed_spans(line, allow)

        identity_hits: list[tuple[int, int, dict]] = []
        regex_hits: list[tuple[int, int, dict]] = []

        for pat, label in block:
            for m in pat.finditer(line):
                if _overlaps(m.start(), m.end(), allowed):
                    continue
                regex_hits.append((m.start(), m.end(), {
                    "line": lineno,
                    "column": m.start(),
                    "matched_text": _truncate(m.group(0)),
                    "pattern_label": label,
                    "identity_key": None,
                }))

        for key, value in identity_secrets.items():
            if not isinstance(value, str) or len(value) < 8:
                continue
            start = 0
            while True:
                idx = line.find(value, start)
                if idx == -1:
                    break
                end = idx + len(value)
                if not _overlaps(idx, end, allowed):
                    identity_hits.append((idx, end, {
                        "line": lineno,
                        "column": idx,
                        "matched_text": _truncate(value),
                        "pattern_label": "Known secret from identity.yaml",
                        "identity_key": key,
                    }))
                start = end

        # Identity matches always win overlaps; regex matches fill the gaps.
        kept: list[tuple[int, int, dict]] = []

        def _add_if_free(items: list[tuple[int, int, dict]]) -> None:
            for s, e, d in sorted(items, key=lambda t: t[0]):
                if not any(s < ke and e > ks for ks, ke, _ in kept):
                    kept.append((s, e, d))

        _add_if_free(identity_hits)
        _add_if_free(regex_hits)

        for _, _, d in kept:
            detections.append(d)

    detections.sort(key=lambda d: (d["line"], d["column"]))
    return detections


def scan_file(
    path: str | Path,
    identity_secrets: dict | None = None,
    extra_block: list[dict] | None = None,
    extra_allow: list[str] | None = None,
) -> list[dict]:
    p = Path(path)
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return []
    dets = scan_content(content, identity_secrets, extra_block, extra_allow)
    for d in dets:
        d["file"] = str(p)
    return dets


def scan_staged_files(
    repo_path: str | Path,
    identity: dict | None = None,
    extra_block: list[dict] | None = None,
    extra_allow: list[str] | None = None,
) -> list[dict]:
    """Scan files staged for commit in ``repo_path``."""
    repo_path = Path(repo_path)
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )
    files = [f for f in proc.stdout.splitlines() if f.strip()]
    secrets = (identity or {}).get("secrets", {}) or {}
    results: list[dict] = []
    for rel in files:
        full = repo_path / rel
        if not full.is_file():
            continue
        results.extend(scan_file(full, secrets, extra_block, extra_allow))
    return results
