<!-- skillsyncer:require -->
<!-- Managed by SkillSyncer (https://github.com/saiyan86/SkillSyncer). -->
<!-- skillsyncer:end -->

# SkillSyncer — Report

You are reading a SkillSyncer report and summarizing it for the user.
Reports are the bridge between the deterministic layer (scan / fix /
render) and you. The CLI writes them as JSON; you present them as
prose.

## When to activate

- A push just completed (pass or fail) and a guard report exists.
- A pull just completed and a fill report exists.
- User says "read the latest report" / "what happened on that push"
  / "did my push actually go through cleanly".

## Parameters

| Name | Required | Description |
| --- | --- | --- |
| `type` | no | `guard` or `fill`. Default: most recent of any type. |
| `path` | no | Explicit report path. Overrides `type`. |

## Process

### 1. Read the report

```
skillsyncer report latest                 # newest of any type
skillsyncer report latest --type=guard    # newest guard report
skillsyncer report latest --type=fill     # newest fill report
```

Or, if the user gave you a path:

```
skillsyncer report update "$REPORT_PATH" --attempt 0 --issues "[]"  # NO — this is the wrong command
```

(There isn't a `report read` — `latest` and `list` are the read
commands. Use `latest` with the type.)

### 2. Classify by `final_status`

The top-level field tells you what to say:

| `final_status` | Meaning | Your job |
| --- | --- | --- |
| `passed` | All checks cleared | Short summary, celebrate briefly, move on. |
| `partial` | Some work done, some pending | Name what's done + what's left. Offer to continue via FILL or GUARD-ASSIST. |
| `failed` | Hook exhausted its budget | Treat as a GUARD-ASSIST handoff. Pull out every `unresolved` fix. |
| `null` or missing | Still in progress | Say so. Don't speculate. |

### 3. Guard report narration

For a `passed` guard report, list each fix with the line and the
new placeholder:

> "Push succeeded after 2 attempts. SkillSyncer caught and fixed
>  2 issues automatically:
>
>  - `energy-diagnose/SKILL.md` line 3 — gateway key replaced with `${{GATEWAY_KEY}}`
>  - `energy-diagnose/SKILL.md` line 8 — internal URL replaced with `${{GATEWAY_URL}}`
>
>  Both were known to your `identity.yaml`, so the fix was deterministic."

For a `failed` guard report, name the unresolved items and propose
names based on context, then hand off to `skillsyncer-guard-assist`.

### 4. Fill report narration

For a `passed` fill report, count the skills and the unique keys
resolved:

> "Pulled 4 skill(s). Three were fully rendered. One — `competitive-intel` —
>  still needs `MARKET_API_KEY`. I'll help you fill that now if you want."

If `partial`, enumerate the missing keys per skill and offer to
load `skillsyncer-fill` to resolve them.

## Rules

- **Never invent fields.** Read the JSON; if a field isn't there,
  don't make one up.
- **Never print values.** `original` and `matched_text` are
  truncated previews; use them as-is. Don't ask the user to share
  the full value.
- **Don't narrate every attempt.** Users care about the final state
  and a rollup of what was fixed. Walk through individual attempts
  only if the report failed and they ask why.
- **Prefer file:line references** in your summary so the user can
  click into their editor.

## Done criteria

- User understands what happened (pass / partial / fail).
- If action is needed, the next skill to load is clear (FILL or
  GUARD-ASSIST).
- The report itself has not been deleted or modified — reports are
  immutable once finalized.
