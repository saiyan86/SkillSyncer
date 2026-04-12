"""Tests for skillsyncer.discoverer."""

from __future__ import annotations

from pathlib import Path

import yaml

from skillsyncer.discoverer import (
    discover,
    _looks_credential,
    _parse_compose_env,
    _parse_env_file,
    _parse_kube_servers,
)


def test_looks_credential_matches_common_names():
    assert _looks_credential("API_KEY")
    assert _looks_credential("GATEWAY_URL")
    assert _looks_credential("FEISHU_WEBHOOK")
    assert _looks_credential("DB_PASSWORD")
    assert _looks_credential("STRIPE_SECRET")


def test_looks_credential_rejects_plain_names():
    assert not _looks_credential("PORT")
    assert not _looks_credential("DEBUG")
    assert not _looks_credential("LANG")


def test_parse_env_file_handles_quotes_and_export(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n"
        "API_KEY=sk-xyz\n"
        'GATEWAY_URL="https://x"\n'
        "export TOKEN_X='abc'\n"
        "BAD\n"  # ignored
        "\n"
    )
    pairs = dict(_parse_env_file(f))
    assert pairs["API_KEY"] == "sk-xyz"
    assert pairs["GATEWAY_URL"] == "https://x"
    assert pairs["TOKEN_X"] == "abc"
    assert "BAD" not in pairs


def test_parse_compose_env_dict_form(tmp_path):
    f = tmp_path / "docker-compose.yml"
    f.write_text(yaml.safe_dump({
        "services": {
            "api": {
                "environment": {
                    "API_KEY": "sk-xyz",
                    "PORT": 8080,
                }
            }
        }
    }))
    pairs = dict(_parse_compose_env(f))
    assert pairs["API_KEY"] == "sk-xyz"
    assert pairs["PORT"] == "8080"


def test_parse_compose_env_list_form(tmp_path):
    f = tmp_path / "docker-compose.yml"
    f.write_text(yaml.safe_dump({
        "services": {
            "api": {"environment": ["TOKEN=abc", "GATEWAY_URL=https://x"]},
        }
    }))
    pairs = dict(_parse_compose_env(f))
    assert pairs["TOKEN"] == "abc"
    assert pairs["GATEWAY_URL"] == "https://x"


def test_parse_kube_servers(tmp_path):
    f = tmp_path / "config"
    f.write_text(yaml.safe_dump({
        "clusters": [
            {"name": "prod", "cluster": {"server": "https://k8s.prod.example.com"}},
            {"name": "dev", "cluster": {"server": "https://k8s.dev.example.com"}},
        ]
    }))
    pairs = dict(_parse_kube_servers(f))
    assert pairs["K8S_PROD_SERVER"] == "https://k8s.prod.example.com"
    assert pairs["K8S_DEV_SERVER"] == "https://k8s.dev.example.com"


def test_discover_credentials_filters_system_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "proj"
    cwd.mkdir()
    env = {
        "PATH": "/usr/bin",   # filtered (system)
        "HOME": "/x",          # filtered (system)
        "API_KEY": "sk-xyz",   # kept
        "GATEWAY_URL": "https://x",  # kept
        "DEBUG": "1",          # filtered (no cred-name match)
    }
    result = discover(home=home, cwd=cwd, env=env)
    keys = {c["key"] for c in result["credentials"]}
    assert "API_KEY" in keys
    assert "GATEWAY_URL" in keys
    assert "PATH" not in keys
    assert "DEBUG" not in keys


def test_discover_collects_env_file_credentials(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / ".env.local").write_text("API_KEY=sk-from-file\nGATEWAY_URL=https://x\nPORT=8080\n")
    result = discover(home=home, cwd=cwd, env={})
    by_key = {c["key"]: c for c in result["credentials"]}
    assert by_key["API_KEY"]["value"] == "sk-from-file"
    assert by_key["API_KEY"]["source"] == ".env.local"
    assert "PORT" not in by_key  # not credential-shaped


def test_discover_dedupes_same_key_value_across_sources(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / ".env").write_text("API_KEY=shared\n")
    env = {"API_KEY": "shared"}
    result = discover(home=home, cwd=cwd, env=env)
    api_entries = [c for c in result["credentials"] if c["key"] == "API_KEY"]
    assert len(api_entries) == 1


def test_discover_agents_includes_known_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude" / "skills").mkdir(parents=True)
    result = discover(home=home, cwd=tmp_path, env={})
    agents = {a["name"]: a for a in result["agents"]}
    assert agents["claude-code"]["found"] is True
    # Other agents are present in the list but not "found".
    assert "cursor" in agents


def test_discover_existing_skills_detects_placeholders(tmp_path):
    home = tmp_path / "home"
    skill_dir = home / ".claude" / "skills" / "energy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Use ${{GATEWAY_KEY}}")
    result = discover(home=home, cwd=tmp_path, env={})
    skills = result["existing_skills"]
    assert len(skills) == 1
    assert skills[0]["name"] == "energy"
    assert skills[0]["agent"] == "claude-code"
    assert skills[0]["has_placeholders"] is True
    assert skills[0]["has_hardcoded_secrets"] is False


def test_discover_existing_skills_detects_hardcoded_secret(tmp_path):
    home = tmp_path / "home"
    skill_dir = home / ".claude" / "skills" / "leaky"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("k=sk-abcdefghij1234567890\n")
    result = discover(home=home, cwd=tmp_path, env={})
    skills = result["existing_skills"]
    assert len(skills) == 1
    assert skills[0]["has_hardcoded_secrets"] is True


def test_discover_returns_full_shape(tmp_path):
    result = discover(home=tmp_path / "home", cwd=tmp_path, env={})
    assert set(result.keys()) == {"agents", "existing_skills", "credentials", "git"}
    assert isinstance(result["git"], dict)
    assert "gh_authenticated" in result["git"]
