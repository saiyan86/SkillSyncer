"""Tests for skillsyncer.filler."""

from __future__ import annotations

from skillsyncer.filler import auto_fill


def test_resolves_from_env():
    skills = {
        "energy": {
            "requires": {"secrets": ["GATEWAY_URL"]},
        }
    }
    found, missing = auto_fill(skills, identity={}, env={"GATEWAY_URL": "https://x"})
    assert found == {"GATEWAY_URL": "https://x"}
    assert missing == {}


def test_skips_already_in_identity():
    skills = {"a": {"requires": {"secrets": ["K"]}}}
    found, missing = auto_fill(skills, identity={"secrets": {"K": "v"}}, env={"K": "from-env"})
    assert found == {}
    assert missing == {}


def test_resolves_from_manifest_values():
    skills = {
        "a": {
            "requires": {"secrets": ["THRESHOLD"]},
            "values": {"THRESHOLD": 0.85},
        }
    }
    found, missing = auto_fill(skills, identity={}, env={})
    assert found == {"THRESHOLD": "0.85"}
    assert missing == {}


def test_cascading_fill_across_skills():
    # Skill A's manifest defines GATEWAY_URL via values; skill B
    # requires it but has no default — should still get filled.
    skills = {
        "a": {
            "requires": {"secrets": ["GATEWAY_URL"]},
            "values": {"GATEWAY_URL": "https://shared"},
        },
        "b": {
            "requires": {"secrets": ["GATEWAY_URL"]},
        },
    }
    found, missing = auto_fill(skills, identity={}, env={})
    assert found == {"GATEWAY_URL": "https://shared"}
    assert missing == {}


def test_dict_form_secret_with_description():
    skills = {
        "a": {
            "requires": {
                "secrets": [
                    {"name": "API_KEY", "description": "Used for auth"},
                ]
            }
        }
    }
    found, missing = auto_fill(skills, identity={}, env={})
    assert found == {}
    assert missing["a"][0]["key"] == "API_KEY"
    assert missing["a"][0]["description"] == "Used for auth"


def test_mixed_string_and_dict_forms():
    skills = {
        "a": {
            "requires": {
                "secrets": [
                    "PLAIN",
                    {"name": "FANCY", "description": "d"},
                ]
            }
        }
    }
    found, missing = auto_fill(skills, identity={}, env={"PLAIN": "p", "FANCY": "f"})
    assert found == {"PLAIN": "p", "FANCY": "f"}


def test_priority_env_beats_manifest_values():
    skills = {
        "a": {
            "requires": {"secrets": ["X"]},
            "values": {"X": "default"},
        }
    }
    found, _ = auto_fill(skills, identity={}, env={"X": "from-env"})
    assert found == {"X": "from-env"}


def test_still_missing_includes_unresolved_only():
    skills = {
        "a": {"requires": {"secrets": ["A_OK", "A_MISSING"]}},
    }
    found, missing = auto_fill(skills, identity={}, env={"A_OK": "ok"})
    assert found == {"A_OK": "ok"}
    assert [m["key"] for m in missing["a"]] == ["A_MISSING"]
