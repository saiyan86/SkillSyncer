<!-- skillsyncer:require -->
<!-- Managed by SkillSyncer (https://github.com/saiyan86/SkillSyncer). -->
<!-- skillsyncer:end -->

# SkillSyncer — Fill

You are the primary filler on pull. After every `git pull`, the
deterministic layer (`skillsyncer fill --auto`) resolves what it can
from env and cascade defaults. You take over for the rest.

Your job: **investigate local sources before asking the user**, then
confirm each candidate before saving, then cascade across skills.

## When to activate

- A skill with `${{KEY}}` placeholders is loaded and the render is
  incomplete.
- `skillsyncer render` just exited 1.
- `skillsyncer report latest --type=fill` shows `partial` status.
- User explicitly asks you to fill missing credentials.

## Parameters

| Name | Required | Description |
| --- | --- | --- |
| `report_path` | no | Path to a fill report. Default: read the latest. |

## Process

### 1. Read the fill report

```
skillsyncer report latest --type=fill
```

Pull out `still_missing` — that's your worklist. Each entry has a
`key`, a `description` (from the skill's `manifest.yaml`), and the
skill name it belongs to.

### 2. For each missing key, investigate before asking

Do **not** ask the user yet. Try in this order:

1. **Shell env** — `echo $<KEY>` (via whatever tool you have).
2. **`.env` files** — `~/.env*`, the project `./.env*`.
3. **AI tool configs** — `~/.openclaw/openclaw.json`, `~/.hermes/.env`,
   `~/.claude/.credentials.json`, Cursor `mcp.json`, etc.
4. **Related skills** — other skills under `~/.claude/skills/` etc.
   sometimes define the same key.
5. **Project READMEs** — the skill you're filling may document
   where its key comes from. Read the README next to it.

Use `skillsyncer init --json --scan-credentials` to get SkillSyncer
to do (1)-(3) for you in a structured way. It returns the locations
and key names it found, without printing values.

### 3. When you find a candidate, confirm before saving

> "Found `GATEWAY_URL=https://gw.site-a.example.com` in `.env.local`.
>  Use it for `energy-diagnose`?"

The user says yes/no/modify. **Read the value yourself — never ask
the user to paste a secret back.**

If they say yes, save it:

```
skillsyncer secret-set GATEWAY_URL <VALUE>
```

Then cascade:

```
skillsyncer fill --auto
skillsyncer render
```

The cascade may have resolved other skills automatically — that's
the whole point. Check the updated fill report and skip any keys
that are now filled.

### 4. When you can't find it, ask the user — once

Use the `description` from the manifest so you can explain what
the secret is for:

> "`market-analysis` needs `MARKET_API_KEY` — the description says
>  'Live market data feed, sign up at marketdata.example.com'.
>  Do you have one? If not, I'll defer this and move on."

### 5. Never block

If the user says "I'll do it later", mark it deferred and move on.
`skillsyncer render` will leave the raw `${{KEY}}` in place; the
skill simply won't run until they answer. That's fine.

## Rules

- **Investigate before asking.** The user hired you so they wouldn't
  have to remember which `.env` file their Slack webhook lives in.
- **Confirm, don't construct.** You find values; the user confirms
  them. You never invent a value.
- **Read values yourself.** Don't ask the user to paste a secret
  into chat. Pull it from the file you found it in.
- **Never block.** Deferral is a valid answer. The skill can wait.
- **Write via the CLI.** `skillsyncer secret-set KEY VALUE` — never
  edit `~/.skillsyncer/identity.yaml` directly.

## Done criteria

- `skillsyncer render` exits 0, OR
- Every remaining unfilled key has been explicitly deferred by the user.
