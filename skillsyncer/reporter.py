"""JSON reports for fill and guard runs.

Reports live at ``~/.skillsyncer/reports/<type>-<timestamp>.json``.
Each report has the same shape regardless of type — the operator skill
reads them to walk the user through what happened on push or pull.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import paths
from ._io import atomic_write

RETENTION_DAYS = 30


def _resolve_dir(reports_dir: str | Path | None) -> Path:
    return Path(reports_dir) if reports_dir else paths.reports_dir()


def create_report(
    report_type: str,
    reports_dir: str | Path | None = None,
    path: str | Path | None = None,
) -> dict:
    """Create a new report dict and persist it. Returns the dict."""
    if path:
        report_path = Path(path)
    else:
        rdir = _resolve_dir(reports_dir)
        timestamp = int(time.time() * 1000)
        report_path = rdir / f"{report_type}-{timestamp}.json"
    report = {
        "type": report_type,
        "created_at": int(time.time()),
        "path": str(report_path),
        "attempts": [],
        "final_status": None,
    }
    _persist(report)
    return report


def update_report(report: dict, attempt: dict) -> None:
    report.setdefault("attempts", []).append(attempt)
    _persist(report)


def finalize_report(
    report: dict,
    status: str,
    retention_days: int = RETENTION_DAYS,
) -> None:
    report["final_status"] = status
    report["finalized_at"] = int(time.time())
    _persist(report)
    _cleanup_old(Path(report["path"]).parent, retention_days)


def latest_report(
    report_type: str | None = None,
    reports_dir: str | Path | None = None,
) -> dict | None:
    rdir = _resolve_dir(reports_dir)
    if not rdir.exists():
        return None
    candidates = sorted(rdir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if report_type:
        candidates = [f for f in candidates if f.name.startswith(f"{report_type}-")]
    if not candidates:
        return None
    return json.loads(candidates[0].read_text(encoding="utf-8"))


def list_reports(reports_dir: str | Path | None = None) -> list[Path]:
    rdir = _resolve_dir(reports_dir)
    if not rdir.exists():
        return []
    return sorted(rdir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)


def read_report(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def clean_old_reports(
    days: int = RETENTION_DAYS,
    reports_dir: str | Path | None = None,
) -> int:
    return _cleanup_old(_resolve_dir(reports_dir), days)


def _persist(report: dict) -> None:
    atomic_write(Path(report["path"]), json.dumps(report, indent=2))


def _cleanup_old(reports_dir: Path, days: int) -> int:
    if not reports_dir.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for f in reports_dir.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed
