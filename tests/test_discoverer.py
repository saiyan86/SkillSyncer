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


def test_walker_descends_arbitrary_depth(tmp_path):
    """No depth cap — find the cred file no matter how deep it lives.

    The filename allowlist + skip-dirs set is the real filter.
    """
    home = tmp_path / "home"
    deep = home / ".openclaw" / "agents" / "research" / "tools" / "alpha" / "v1" / "config"
    deep.mkdir(parents=True)
    (deep / ".env").write_text("RESEARCH_API_KEY=rsa-deep-1234567890\n")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "RESEARCH_API_KEY" in keys


def test_openclaw_json_deeply_nested_creds(tmp_path):
    """The real shape we hit on a live OpenClaw machine: credentials
    are scattered across deeply nested paths under custom names.
    The recursive extractor + path-based name synthesis must find
    them all."""
    home = tmp_path / "home"
    openclaw = home / ".openclaw"
    openclaw.mkdir(parents=True)
    (openclaw / "openclaw.json").write_text("""
{
  "channels": {
    "feishu": {"appSecret": "feishu-app-secret-xxxx"},
    "msteams": {"appPassword": "ms-app-password-yyyy"}
  },
  "gateway": {
    "auth": {
      "token": "gateway-token-zzz",
      "password": "gateway-pw-aaa"
    }
  },
  "skills": {
    "entries": {
      "goplaces": {"apiKey": "goplaces-key-bbb"},
      "agentmail": {"env": {"AGENTMAIL_API_KEY": "agentmail-key-ccc"}}
    }
  },
  "models": {
    "providers": {
      "minimax": {
        "apiKey": "minimax-key-ddd",
        "baseUrl": "https://api.minimax.chat/v1"
      }
    }
  },
  "plugins": {
    "entries": {
      "brave": {
        "config": {
          "webSearch": {"apiKey": "brave-search-key-eee"}
        }
      }
    }
  },
  "env": {
    "MINIMAX_API_KEY": "top-level-env-fff"
  }
}
""")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}

    # Path-synthesized names from nested apiKey/secret/token leaves.
    assert "BRAVE_API_KEY" in keys              # plugins.entries.brave.…apiKey
    assert "GOPLACES_API_KEY" in keys           # skills.entries.goplaces.apiKey
    assert "MINIMAX_API_KEY" in keys            # models.providers.minimax.apiKey
    assert "FEISHU_APP_SECRET" in keys          # channels.feishu.appSecret
    assert "MSTEAMS_APP_PASSWORD" in keys       # channels.msteams.appPassword
    assert "GATEWAY_TOKEN" in keys              # gateway.auth.token (auth is generic)
    assert "GATEWAY_PASSWORD" in keys           # gateway.auth.password

    # Already-envvar-style nested under env: kept as-is.
    assert "AGENTMAIL_API_KEY" in keys


def test_recursive_extractor_skips_non_credential_keys(tmp_path):
    """preKey, registrationId, etc. must NOT be picked up just
    because they contain 'Key'/'Id'."""
    home = tmp_path / "home"
    openclaw = home / ".openclaw"
    openclaw.mkdir(parents=True)
    (openclaw / "session.json").write_text("""
{
  "remoteJid": "user@example.com",
  "registrationId": 12345,
  "preKey": {"id": 1, "publicKey": "abc"},
  "noiseKey": "noise-value"
}
""")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    # None of these should leak through.
    for k in ("REMOTEJID", "REGISTRATION_ID", "PRE_KEY", "NOISE_KEY"):
        assert k not in keys


def test_recursive_extractor_skips_boring_files(tmp_path):
    """package.json must not be parsed even if it contains
    credential-shaped keys (which it occasionally does in scripts)."""
    home = tmp_path / "home"
    cursor = home / ".cursor"
    cursor.mkdir(parents=True)
    (cursor / "package.json").write_text("""
{
  "scripts": {
    "deploy": "API_KEY=xxx npm run real"
  },
  "config": {
    "apiKey": "should-not-appear"
  }
}
""")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "CONFIG_API_KEY" not in keys
    assert "PACKAGE_API_KEY" not in keys


def test_openclaw_auth_profiles_json(tmp_path):
    """openclaw stores per-agent auth credentials at
    ~/.openclaw/agents/<id>/agent/auth-profiles.json. Confirmed
    against openclaw/openclaw src/agents/auth-profiles/paths.ts."""
    home = tmp_path / "home"
    auth = home / ".openclaw" / "agents" / "feishu" / "agent"
    auth.mkdir(parents=True)
    (auth / "auth-profiles.json").write_text("""
{
  "version": 1,
  "profiles": {
    "anthropic:manual": {
      "provider": "anthropic",
      "token": "ant-token-fake-1234567890"
    },
    "openai-codex:default": {
      "provider": "openai-codex",
      "apiKey": "sk-codex-fake-1234567890"
    }
  }
}
""")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    # synthesized via 'profiles' container → entity = profile id
    assert any("ANTHROPIC" in k and "TOKEN" in k for k in keys), keys
    assert any("OPENAI_CODEX" in k or "CODEX" in k for k in keys), keys


def test_openclaw_models_json_provider_keys(tmp_path):
    """openclaw stores generated provider configs at
    ~/.openclaw/agents/<id>/agent/models.json with provider apiKey
    fields. Confirmed against the secrets-audit doc."""
    home = tmp_path / "home"
    agent = home / ".openclaw" / "agents" / "default" / "agent"
    agent.mkdir(parents=True)
    (agent / "models.json").write_text("""
{
  "providers": {
    "minimax": {"apiKey": "minimax-fake-key-xxx"},
    "minimax-cn": {"apiKey": "minimax-cn-fake-key-yyy"}
  }
}
""")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "MINIMAX_API_KEY" in keys
    # camelCase to snake: "minimax-cn" -> hyphen normalized to _
    assert "MINIMAX_CN_API_KEY" in keys


def test_openclaw_legacy_clawdbot_dir(tmp_path):
    """openclaw still reads ~/.clawdbot/ as a legacy state dir."""
    home = tmp_path / "home"
    legacy = home / ".clawdbot"
    legacy.mkdir(parents=True)
    (legacy / "clawdbot.json").write_text(
        '{"plugins":{"entries":{"brave":{"config":{"webSearch":{"apiKey":"legacy-brave-xxx"}}}}}}'
    )
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "BRAVE_API_KEY" in keys


def test_openclaw_home_env_override(tmp_path):
    """OPENCLAW_HOME / OPENCLAW_STATE_DIR move the state dir;
    discoverer follows."""
    home = tmp_path / "home"
    home.mkdir()
    custom = tmp_path / "custom-openclaw"
    custom.mkdir()
    (custom / "openclaw.json").write_text(
        '{"plugins":{"entries":{"brave":{"config":{"webSearch":{"apiKey":"custom-brave-xxx"}}}}}}'
    )
    result = discover(
        home=home, cwd=tmp_path,
        env={"OPENCLAW_STATE_DIR": str(custom)},
    )
    keys = {c["key"] for c in result["credentials"]}
    assert "BRAVE_API_KEY" in keys


def test_hermes_real_shape(tmp_path):
    """hermes-agent uses ~/.hermes/config.yaml + ~/.hermes/.env.
    Both must surface. Confirmed against NousResearch/hermes-agent
    hermes_cli/config.py module docstring."""
    home = tmp_path / "home"
    hermes = home / ".hermes"
    hermes.mkdir(parents=True)
    (hermes / ".env").write_text(
        "OPENROUTER_API_KEY=or-fake-key-xxx\n"
        "HF_TOKEN=hf_fake_token_xxx\n"
    )
    (hermes / "config.yaml").write_text("""
model:
  default: anthropic/claude-opus-4.6
  provider: openrouter
""")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "OPENROUTER_API_KEY" in keys
    assert "HF_TOKEN" in keys


def test_synthesized_names_match_placeholder_grammar(tmp_path):
    """Synthesized names must satisfy ``[A-Z_][A-Z0-9_]*`` so they
    can actually be used in ${{...}} placeholders. Profile ids like
    ``anthropic:manual`` must be normalized."""
    home = tmp_path / "home"
    auth = home / ".openclaw" / "agents" / "feishu" / "agent"
    auth.mkdir(parents=True)
    (auth / "auth-profiles.json").write_text("""
{
  "profiles": {
    "anthropic:manual": {"token": "ant-token-fake-1234567890"}
  }
}
""")
    import re as _re
    grammar = _re.compile(r"^[A-Z_][A-Z0-9_]*$")
    result = discover(home=home, cwd=tmp_path, env={})
    for c in result["credentials"]:
        assert grammar.match(c["key"]), f"key {c['key']!r} would be rejected by ${{{{...}}}}"


def test_filename_stem_used_when_no_path_entity(tmp_path):
    """Bare ``token`` in ``auth.json`` becomes ``AUTH_TOKEN``,
    not just ``TOKEN``."""
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "auth.json").write_text("""
{
  "tokens": {
    "access_token": "access-fake-1234567890",
    "id_token": "id-fake-1234567890",
    "refresh_token": "refresh-fake-1234567890"
  }
}
""")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "AUTH_ACCESS_TOKEN" in keys
    assert "AUTH_ID_TOKEN" in keys
    assert "AUTH_REFRESH_TOKEN" in keys
    # The bare ones should NOT appear.
    assert "ACCESS_TOKEN" not in keys
    assert "REFRESH_TOKEN" not in keys


def test_msal_token_cache_files_skipped(tmp_path):
    """m365-token-cache.json and similar must NOT pollute the
    credential list — they're protocol caches with synthesized
    UUID-shaped keys."""
    home = tmp_path / "home"
    cred_dir = home / ".openclaw" / "credentials"
    cred_dir.mkdir(parents=True)
    (cred_dir / "m365-token-cache.json").write_text("""
{
  "AccessToken": {
    "very-long-uuid-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbbbbbb-cccccccccccccccc": {
      "secret": "should-not-appear"
    }
  }
}
""")
    result = discover(home=home, cwd=tmp_path, env={})
    # The synthesized name from this path would be > 64 chars and
    # the filename matches the cache noise pattern. Either filter
    # is enough; both apply here.
    for c in result["credentials"]:
        assert "m365-token-cache" not in c["source"], c
        assert len(c["key"]) <= 64, c["key"]


def test_whatsapp_session_files_skipped(tmp_path):
    """WhatsApp session-*.json / pre-key-*.json are protocol primitives,
    not user credentials, even though they contain key/secret-named fields."""
    home = tmp_path / "home"
    wa = home / ".openclaw" / "credentials" / "whatsapp" / "default"
    wa.mkdir(parents=True)
    (wa / "session-1234567890_1.0.json").write_text(
        '{"currentRatchet": {"rootKey": "ratchet-noise-xxx"}}'
    )
    (wa / "pre-key-2771.json").write_text(
        '{"keyPair": {"private": "private-key-noise-xxx"}}'
    )
    result = discover(home=home, cwd=tmp_path, env={})
    for c in result["credentials"]:
        assert "session-" not in c["source"], c
        assert "pre-key-" not in c["source"], c


def test_arbitrary_yaml_filename_is_scanned(tmp_path):
    """The point of dropping the filename allowlist: ANY .yaml in
    an AI tool dir is parsed, regardless of what it's called."""
    home = tmp_path / "home"
    custom = home / ".openclaw" / "weird-custom-name.yaml"
    custom.parent.mkdir(parents=True)
    custom.write_text("""
plugins:
  entries:
    brave:
      apiKey: brave-from-yaml-xxx
""")
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "BRAVE_API_KEY" in keys


def test_walker_safe_against_symlink_loops(tmp_path):
    """A symlink that points back at an ancestor must not hang the scan."""
    home = tmp_path / "home"
    deep = home / ".openclaw" / "real"
    deep.mkdir(parents=True)
    (deep / ".env").write_text("REAL_KEY=actual-value\n")
    loop = home / ".openclaw" / "loop"
    try:
        loop.symlink_to(home / ".openclaw")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this filesystem")
    # If the walker followed symlinks this would never return.
    result = discover(home=home, cwd=tmp_path, env={})
    keys = {c["key"] for c in result["credentials"]}
    assert "REAL_KEY" in keys


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
