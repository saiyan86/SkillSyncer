"""Tests for skillsyncer.scanner."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skillsyncer.scanner import scan_content, scan_staged_files


def test_detects_api_key():
    dets = scan_content("API = sk-abcdefghij1234567890", {})
    assert len(dets) == 1
    assert dets[0]["pattern_label"] == "API key"
    assert dets[0]["line"] == 1
    assert dets[0]["identity_key"] is None


def test_detects_bearer_token():
    dets = scan_content("Authorization: Bearer abc.def.ghijk-LMNOPQRST_uv", {})
    assert any(d["pattern_label"] == "Bearer token" for d in dets)


def test_detects_credentials_in_url():
    dets = scan_content("https://user:p4ssword@example.com/api", {})
    assert any(d["pattern_label"] == "Credentials in URL" for d in dets)


def test_detects_aws_access_key():
    dets = scan_content("AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP", {})
    assert any(d["pattern_label"] == "AWS access key" for d in dets)


def test_detects_private_key_header():
    dets = scan_content("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...", {})
    assert any(d["pattern_label"] == "Private key" for d in dets)


def test_detects_github_pat():
    pat = "ghp_" + "A" * 36
    dets = scan_content(f"token: {pat}", {})
    assert any(d["pattern_label"] == "GitHub personal access token" for d in dets)


def test_detects_slack_token():
    dets = scan_content("slack: xoxb-1234567890-abcdef", {})
    assert any(d["pattern_label"] == "Slack token" for d in dets)


def test_placeholder_excluded_from_detection():
    # Identity literal lives inside a placeholder — must NOT be flagged.
    secrets = {"GATEWAY_KEY": "sk-abcdefghij1234567890"}
    content = "Use ${{GATEWAY_KEY}} for auth."
    dets = scan_content(content, secrets)
    assert dets == []


def test_plain_text_no_false_positives():
    content = "This is a normal sentence with no secrets at all.\nJust prose."
    assert scan_content(content, {}) == []


def test_identity_cross_reference_match():
    secrets = {"GATEWAY_URL": "https://gw.site-a.example.com"}
    content = "Connect to https://gw.site-a.example.com/api"
    dets = scan_content(content, secrets)
    assert len(dets) == 1
    assert dets[0]["identity_key"] == "GATEWAY_URL"
    assert dets[0]["pattern_label"] == "Known secret from identity.yaml"


def test_identity_short_value_skipped():
    # Values < 8 chars must not produce identity matches (false positive risk).
    secrets = {"SHORT": "abc"}
    dets = scan_content("the abc lives here", secrets)
    assert dets == []


def test_overlap_prefers_identity_match():
    # Regex would also match this; identity match must win and be the
    # only detection so guarder gets the identity_key.
    secrets = {"GATEWAY_KEY": "sk-abcdefghij1234567890"}
    content = "key=sk-abcdefghij1234567890"
    dets = scan_content(content, secrets)
    assert len(dets) == 1
    assert dets[0]["identity_key"] == "GATEWAY_KEY"


def test_line_and_column_are_correct():
    content = "line one\nfoo sk-abcdefghij1234567890 bar"
    dets = scan_content(content, {})
    assert len(dets) == 1
    assert dets[0]["line"] == 2
    assert dets[0]["column"] == 4


def test_extra_block_pattern():
    extra = [{"pattern": r"COMPANY-[0-9]{6}", "label": "Company ID"}]
    dets = scan_content("ID: COMPANY-123456", {}, extra_block=extra)
    assert any(d["pattern_label"] == "Company ID" for d in dets)


def test_extra_allow_pattern():
    extra_allow = [r"COMPANY-PLACEHOLDER"]
    secrets = {"X": "COMPANY-PLACEHOLDER-value"}
    # Without the allow pattern, identity cross-ref would catch it.
    dets = scan_content("token COMPANY-PLACEHOLDER-value here", secrets, extra_allow=extra_allow)
    assert dets == []


def test_empty_content():
    assert scan_content("", {"X": "very-secret-value"}) == []


def test_scan_staged_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    leaky = repo / "leaky.md"
    leaky.write_text("token sk-abcdefghij1234567890\n")
    clean = repo / "clean.md"
    clean.write_text("nothing here\n")
    subprocess.run(["git", "add", "leaky.md", "clean.md"], cwd=repo, check=True)
    dets = scan_staged_files(repo, {"secrets": {}})
    assert len(dets) == 1
    assert "leaky.md" in dets[0]["file"]
