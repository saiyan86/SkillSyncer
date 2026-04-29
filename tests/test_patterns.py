"""Tests for skillsyncer.patterns.

These tests guard the regex catalogue itself: that every entry has the
expected shape, compiles, and that the ordering invariant (specific
labels before generic catch-alls) is preserved. The behavioural tests
for what the scanner actually catches live in test_scanner.py.
"""

from __future__ import annotations

import re

from skillsyncer.patterns import ALLOW_PATTERNS, BLOCK_PATTERNS


def test_block_patterns_have_required_keys():
    for entry in BLOCK_PATTERNS:
        assert isinstance(entry, dict)
        assert set(entry.keys()) >= {"pattern", "label"}
        assert isinstance(entry["pattern"], str) and entry["pattern"]
        assert isinstance(entry["label"], str) and entry["label"]


def test_block_patterns_compile():
    for entry in BLOCK_PATTERNS:
        re.compile(entry["pattern"])


def test_allow_patterns_compile():
    for pat in ALLOW_PATTERNS:
        re.compile(pat)


def test_block_labels_are_unique():
    labels = [e["label"] for e in BLOCK_PATTERNS]
    assert len(labels) == len(set(labels)), f"duplicate label in BLOCK_PATTERNS: {labels}"


def test_specific_patterns_precede_generic_api_key():
    """The generic ``API key`` catch-all must come AFTER provider-specific
    patterns. The scanner's overlap dedup keeps the first match, so reversing
    this ordering would mislabel real provider keys."""
    labels = [e["label"] for e in BLOCK_PATTERNS]
    generic_idx = labels.index("API key")
    for specific in (
        "Anthropic API key",
        "OpenAI project key",
        "Google API key",
        "xAI API key",
        "Groq API key",
        "HuggingFace token",
        "Replicate API token",
        "Perplexity API key",
    ):
        assert labels.index(specific) < generic_idx, (
            f"{specific!r} must appear before generic 'API key' "
            "to win regex overlap dedup"
        )


def test_skillsyncer_placeholder_is_in_allow():
    """The ``${{KEY}}`` placeholder must be allowed so rendered templates
    don't trip the scanner against their own placeholders."""
    placeholder = "${{GATEWAY_KEY}}"
    assert any(re.search(p, placeholder) for p in ALLOW_PATTERNS)


def test_allow_does_not_match_real_secret():
    """The allow regex is anchored on the placeholder shape; a raw key
    must not be allow-listed by accident."""
    real = "sk-ant-" + "A" * 50
    assert not any(re.search(p, real) for p in ALLOW_PATTERNS)


def test_each_pattern_matches_at_least_one_realistic_example():
    """Each provider pattern should match a synthetic but plausible key.
    Catches typos in the regex (e.g. dropped a character class)."""
    examples = {
        "Anthropic API key": "sk-ant-" + "A" * 50,
        "OpenAI project key": "sk-proj-" + "B" * 50,
        "Google API key": "AIza" + "C" * 35,
        "xAI API key": "xai-" + "D" * 40,
        "Groq API key": "gsk_" + "E" * 40,
        "HuggingFace token": "hf_" + "F" * 30,
        "Replicate API token": "r8_" + "G" * 30,
        "Perplexity API key": "pplx-" + "H" * 30,
        "AWS access key": "AKIAABCDEFGHIJKLMNOP",
        "GitHub personal access token": "ghp_" + "I" * 36,
        "Slack token": "xoxb-1234567890-abcdef",
        "API key": "sk-aaaaaaaaaa",
        "Bearer token": "Bearer " + "J" * 30,
        "Credentials in URL": "https://user:pass@example.com",
        "Private key": "-----BEGIN PRIVATE KEY-----",
    }
    by_label = {e["label"]: e["pattern"] for e in BLOCK_PATTERNS}
    for label, sample in examples.items():
        assert label in by_label, f"missing pattern for label {label!r}"
        assert re.search(by_label[label], sample), (
            f"pattern for {label!r} does not match its sample input"
        )
