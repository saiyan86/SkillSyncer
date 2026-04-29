"""Tests for skillsyncer.reporter."""

from __future__ import annotations

import json
import os
import time

from skillsyncer.reporter import (
    clean_old_reports,
    create_report,
    finalize_report,
    latest_report,
    list_reports,
    read_report,
    update_report,
)


def test_create_report_persists(tmp_path):
    r = create_report("guard", reports_dir=tmp_path)
    assert r["type"] == "guard"
    assert r["final_status"] is None
    p = tmp_path / r["path"].split("/")[-1]
    assert p.exists()
    on_disk = json.loads(p.read_text())
    assert on_disk["type"] == "guard"


def test_update_and_finalize(tmp_path):
    r = create_report("guard", reports_dir=tmp_path)
    update_report(r, {"attempt": 1, "issues": ["x"]})
    update_report(r, {"attempt": 2, "issues": []})
    finalize_report(r, status="passed")

    on_disk = read_report(r["path"])
    assert on_disk["final_status"] == "passed"
    assert len(on_disk["attempts"]) == 2
    assert "finalized_at" in on_disk


def test_latest_report_filters_by_type(tmp_path):
    g = create_report("guard", reports_dir=tmp_path)
    time.sleep(0.01)
    f = create_report("fill", reports_dir=tmp_path)
    assert latest_report("fill", reports_dir=tmp_path)["path"] == f["path"]
    assert latest_report("guard", reports_dir=tmp_path)["path"] == g["path"]
    # No filter → most recent overall.
    assert latest_report(reports_dir=tmp_path)["path"] == f["path"]


def test_latest_report_none_when_empty(tmp_path):
    assert latest_report(reports_dir=tmp_path) is None


def test_list_reports_orders_newest_first(tmp_path):
    a = create_report("guard", reports_dir=tmp_path)
    time.sleep(0.01)
    b = create_report("guard", reports_dir=tmp_path)
    listed = [str(p) for p in list_reports(reports_dir=tmp_path)]
    assert listed[0] == b["path"]
    assert listed[1] == a["path"]


def test_finalize_with_explicit_path(tmp_path):
    target = tmp_path / "subdir" / "explicit.json"
    r = create_report("fill", path=target)
    finalize_report(r, status="partial")
    assert target.exists()
    assert read_report(target)["final_status"] == "partial"


def test_list_reports_returns_empty_when_dir_missing(tmp_path):
    assert list_reports(reports_dir=tmp_path / "nope") == []


def test_clean_old_reports_removes_only_aged(tmp_path):
    fresh = create_report("guard", reports_dir=tmp_path)
    time.sleep(0.01)
    aged = create_report("guard", reports_dir=tmp_path)
    aged_path = tmp_path / aged["path"].split("/")[-1]
    # Backdate the aged report 60 days into the past.
    old = time.time() - 60 * 86400
    os.utime(aged_path, (old, old))

    removed = clean_old_reports(days=30, reports_dir=tmp_path)
    assert removed == 1
    fresh_path = tmp_path / fresh["path"].split("/")[-1]
    assert fresh_path.exists()
    assert not aged_path.exists()


def test_clean_old_reports_handles_missing_dir(tmp_path):
    assert clean_old_reports(days=1, reports_dir=tmp_path / "missing") == 0


def test_finalize_triggers_retention_cleanup(tmp_path):
    aged = create_report("guard", reports_dir=tmp_path)
    aged_path = tmp_path / aged["path"].split("/")[-1]
    old = time.time() - 60 * 86400
    os.utime(aged_path, (old, old))

    time.sleep(0.01)
    fresh = create_report("guard", reports_dir=tmp_path)
    finalize_report(fresh, status="passed", retention_days=30)

    assert not aged_path.exists()
    fresh_path = tmp_path / fresh["path"].split("/")[-1]
    assert fresh_path.exists()


def test_create_report_unique_paths(tmp_path):
    """Distinct create_report calls must produce distinct files —
    otherwise finalize_report on one would clobber the other."""
    seen = set()
    for _ in range(5):
        r = create_report("guard", reports_dir=tmp_path)
        seen.add(r["path"])
        # The timestamp is millisecond-precision; sleep just over one
        # tick so consecutive calls don't collide.
        time.sleep(0.002)
    assert len(seen) == 5


def test_read_report_roundtrip(tmp_path):
    r = create_report("guard", reports_dir=tmp_path)
    update_report(r, {"attempt": 1, "issues": ["a", "b"]})
    on_disk = read_report(r["path"])
    assert on_disk["attempts"][0]["issues"] == ["a", "b"]
