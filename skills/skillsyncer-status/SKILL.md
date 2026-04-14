<!-- skillsyncer:require -->
<!-- Managed by SkillSyncer (https://github.com/saiyan86/SkillSyncer). -->
<!-- skillsyncer:end -->

# SkillSyncer — Status

You are answering "what's the state of my SkillSyncer setup".

## When to activate

- User says "status" / "what skills do I have" / "show me my
  setup" / "is everything working".

## Process

### 1. Pull three sources

```
skillsyncer status           # home, sources, targets, per-skill drift
skillsyncer skills --json    # flat list of every local skill
skillsyncer doctor           # health check with ✓ / ✗ per component
```

These cover:

- Config: home path, source count, target count, stored secret count.
- Skills: every local skill grouped by agent, with tags
  (`placeholders`, `hardcoded-secret`, `skillsyncer-managed`).
- Health: whether git is on PATH, whether hooks are installed in
  each source, whether every target dir exists.

### 2. Present the headline first

> "You have **12 skills** across **claude-code** and **cursor**,
>  tracked by **1 source** (`yc-skills`). **11 are fully synced**;
>  `market-analysis` is missing `MARKET_API_KEY`. One hook issue:
>  the pre-push hook in `yc-skills` is missing."

### 3. Then offer the next action

Pick one based on what you saw:

- Missing secrets → "Want me to help fill them? (FILL flow)"
- Missing hook → "Want me to install the hook now? `skillsyncer hooks install --path ...`"
- Drift detected (skills changed since last render) →
  "Want me to sync and re-render? `skillsyncer sync`"
- Everything is healthy → "Everything looks good. You can keep
  working — SkillSyncer runs in the background on push/pull."

### 4. Offer the drill-down

If the user asks "tell me more about X", use:

```
skillsyncer skill show <name>       # per-skill detail
skillsyncer sources show <name>     # per-source detail
```

## Rules

- **Don't dump the raw output.** The CLI already prints a formatted
  table. Your job is to narrate it and propose next actions, not
  echo it back verbatim.
- **Never print credential values.** Key counts and names only.
- **Pick one next action, not five.** The user came to you for a
  clear "do this next" — not a menu.

## Done criteria

- User knows:
  - how many skills they have and which agents they're under,
  - which (if any) are unhealthy,
  - what the single best next action is.
