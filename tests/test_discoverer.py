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
    assert set(result.keys()) == {
        "agents", "existing_skills", "credentials",
        "credential_scan_plan", "credential_scan_performed", "git",
    }
    assert isinstance(result["git"], dict)
    assert "gh_authenticated" in result["git"]
    assert result["credential_scan_performed"] is True
    assert isinstance(result["credential_scan_plan"], list)


def test_discover_skips_creds_when_scan_disabled(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("API_KEY=should-not-appear\n")
    result = discover(home=home, cwd=tmp_path, env={"OPENAI_KEY": "x"}, scan_credentials=False)
    assert result["credentials"] == []
    assert result["credential_scan_performed"] is False
    # The plan still tells callers what *would* have been scanned.
    paths = {p["display"] for p in result["credential_scan_plan"]}
    assert any("env" in d for d in paths)


def test_mcp_servers_env_block_extracted_from_json(tmp_path):
    """Claude Desktop / OpenClaw / Cursor / Continue all use this shape."""
    home = tmp_path / "home"
    openclaw = home / ".openclaw"
    openclaw.mkdir(parents=True)
    (openclaw / "mcp.json").write_text("""
{
  "mcpServers": {
    "brave-search": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-brave-search"],
      "env": {
        "BRAVE_API_KEY": "BSAFAKEFAKEFAKEFAKEFAKEFAKE"
      }
    },
    "google-places": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-google-places"],
      "env": {
        "GOOGLE_PLACES_API_KEY": "AIzaFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE"
      }
    }
  }
}
""")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "BRAVE_API_KEY" in keys
    assert "GOOGLE_PLACES_API_KEY" in keys


def test_mcp_servers_env_block_in_yaml(tmp_path):
    home = tmp_path / "home"
    cursor = home / ".cursor"
    cursor.mkdir(parents=True)
    (cursor / "mcp_servers.json").write_text("""
{
  "servers": {
    "brave": {"env": {"BRAVE_API_KEY": "bsa-xxx"}}
  }
}
""")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "BRAVE_API_KEY" in keys


def test_walker_descends_three_levels(tmp_path):
    """Per-agent profile dirs nested 2-3 levels deep are still scanned."""
    home = tmp_path / "home"
    deep = home / ".openclaw" / "agents" / "research" / "tools"
    deep.mkdir(parents=True)
    (deep / ".env").write_text("RESEARCH_API_KEY=rsa-deep-1234567890\n")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "RESEARCH_API_KEY" in keys


def test_walker_skips_noise_dirs(tmp_path):
    """node_modules and friends must not get walked."""
    home = tmp_path / "home"
    noisy = home / ".openclaw" / "node_modules" / "deep"
    noisy.mkdir(parents=True)
    (noisy / ".env").write_text("LEAKED=should-not-appear\n")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "LEAKED" not in keys


def test_walker_skips_hidden_subdirs_under_root(tmp_path):
    home = tmp_path / "home"
    cache = home / ".openclaw" / ".cache" / "deep"
    cache.mkdir(parents=True)
    (cache / ".env").write_text("HIDDEN_KEY=should-not-appear\n")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "HIDDEN_KEY" not in keys


def test_credential_scan_plan_includes_ai_tool_dirs(tmp_path):
    home = tmp_path / "home"
    (home / ".openclaw").mkdir(parents=True)
    result = discover(home=home, cwd=tmp_path, env={})
    plan = result["credential_scan_plan"]
    by_kind = {}
    for p in plan:
        by_kind.setdefault(p["kind"], []).append(p)
    assert "ai-tool" in by_kind
    assert any(".openclaw" in p["display"] and p["exists"] for p in by_kind["ai-tool"])


def test_existing_skills_dedup_and_depth_one(tmp_path):
    """Plugin bundles must not produce 9x duplicates."""
    home = tmp_path / "home"
    skills_dir = home / ".claude" / "skills"
    # Top-level skill — should be picked up.
    (skills_dir / "energy").mkdir(parents=True)
    (skills_dir / "energy" / "SKILL.md").write_text("hi")
    # A plugin bundle that nests another SKILL.md two levels deep —
    # the depth-1 walker should ignore it.
    (skills_dir / "plugin-bundle" / "nested-skill").mkdir(parents=True)
    (skills_dir / "plugin-bundle" / "nested-skill" / "SKILL.md").write_text("hi")
    # The plugin-bundle dir itself has no SKILL.md → not a skill.
    result = discover(home=home, cwd=tmp_path, env={})
    names = [s["name"] for s in result["existing_skills"]]
    assert names == ["energy"]


def test_openclaw_skills_under_dotopenclaw(tmp_path):
    home = tmp_path / "home"
    skills = home / ".openclaw" / "skills" / "diagnose"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("Use ${{GATEWAY_KEY}}")
    result = discover(home=home, cwd=tmp_path, env={})
    agents = {a["name"]: a for a in result["agents"]}
    assert agents["openclaw"]["found"] is True
    assert "diagnose" in [s["name"] for s in result["existing_skills"]]


def test_hermes_and_cowork_listed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = discover(home=home, cwd=tmp_path, env={})
    names = [a["name"] for a in result["agents"]]
    assert "claude-cowork" in names
    assert "hermes" in names
    assert "openclaw" in names


def test_credentials_from_openclaw_env(tmp_path):
    home = tmp_path / "home"
    openclaw = home / ".openclaw"
    openclaw.mkdir(parents=True)
    (openclaw / ".env").write_text("FEISHU_WEBHOOK=https://feishu.x/webhook\n")
    result = discover(home=home, cwd=tmp_path, env={})
    by_key = {c["key"]: c for c in result["credentials"]}
    assert "FEISHU_WEBHOOK" in by_key
    assert ".openclaw/.env" in by_key["FEISHU_WEBHOOK"]["source"]


def test_credentials_from_anthropic_config_yaml(tmp_path):
    home = tmp_path / "home"
    cfg_dir = home / ".config" / "anthropic"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text(
        "secrets:\n  ANTHROPIC_API_KEY: sk-ant-fake-1234567890123456789012345678901234567890\n"
    )
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "ANTHROPIC_API_KEY" in keys


def test_credentials_descend_one_level_into_subdirs(tmp_path):
    """Walker descends one level past each AI tool dir so per-profile
    config dirs (e.g. ``~/.openclaw/default/credentials``) are caught."""
    home = tmp_path / "home"
    sub = home / ".openclaw" / "default"
    sub.mkdir(parents=True)
    (sub / "credentials").write_text("API_KEY=sk-from-profile\n")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "API_KEY" in keys


def test_credentials_pattern_catches_ai_provider_names(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-xyz",
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "OPENAI_API_KEY": "sk-xyz",
        "GROQ_API_KEY": "gsk_xyz",
        "MISTRAL_API_KEY": "mst-xyz",
        "STEPONEAI_API_KEY": "step-xyz",
        "PATH": "/usr/bin",
    }
    result = discover(home=home, cwd=tmp_path, env=env)
    keys = {c["key"] for c in result["credentials"]}
    assert "ANTHROPIC_API_KEY" in keys
    assert "ANTHROPIC_BASE_URL" in keys
    assert "OPENAI_API_KEY" in keys
    assert "GROQ_API_KEY" in keys
    assert "MISTRAL_API_KEY" in keys
    assert "STEPONEAI_API_KEY" in keys
    assert "PATH" not in keys
