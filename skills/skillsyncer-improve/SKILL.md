<!-- skillsyncer:require -->
<!-- Managed by SkillSyncer (https://github.com/saiyan86/SkillSyncer). -->
<!-- skillsyncer:end -->

# SkillSyncer — Improve

This is the learning loop. You read what happened recently, find
patterns in what keeps breaking, and propose rewrites to config so
the next run stops tripping on the same thing.

The goal is the compound curve. Every session you run this, the
system gets a little better. The fat skills don't degrade; the
judgment you encode here becomes permanent.

## When to activate

- User says "improve SkillSyncer" / "make it stop asking me the
  same thing" / "tighten up my setup".
- End of a working session.
- Cron: nightly at 3am while the user is asleep.

## Parameters

| Name | Required | Description |
| --- | --- | --- |
| `WINDOW` | no | Time window for reports. Default `7d`. |
| `DRY_RUN` | no | If `true`, print proposed changes without writing. |

## Process

### Step 1 — Read every report in WINDOW

```
skillsyncer report list
```

Keep the ones within WINDOW. For each, pull the full JSON via
`skillsyncer report latest` (when you need the newest) or read the
file directly.

### Step 2 — Diarize per-report

For each report, write a 1-line signature:

```
guard/1733...  failed   handler.py:45  "high-entropy" unresolved
guard/1733...  failed   handler.py:45  "high-entropy" unresolved   ← SAME
guard/1734...  passed   templated ${{GATEWAY_KEY}} ×2
fill/1734...   partial  market-analysis needs MARKET_API_KEY
fill/1735...   partial  market-analysis needs MARKET_API_KEY         ← SAME
```

### Step 3 — Find recurring patterns

Group signatures. Anything that repeats ≥ 3 times in WINDOW is a
**drift source** — an area where SkillSyncer keeps asking the user
the same question.

For each drift source, classify into one of three buckets:

1. **Repeated false positive** → propose an `allow_patterns` entry
   for `config.yaml`.
2. **Repeated real secret that's never saved** → propose a
   `skillsyncer secret-set KEY <value>` with the value read from
   wherever it keeps being discovered.
3. **Repeated missing secret that no source has** → propose
   asking the user for it ONCE, then saving it.

### Step 4 — Propose changes

For each drift source, print the exact diff you'd write:

```
--- ~/.skillsyncer/config.yaml (proposed)
+++ ~/.skillsyncer/config.yaml (current)
@@
 allow_patterns:
+  - "a8f2c9d3[a-f0-9]{32}"   # content hash in handler.py (rationale: 3 guard reports in 7d classified this as non-secret)
```

Cite the specific reports that justify each proposal.

### Step 5 — Ask the user to confirm

In the style:

> "I found 3 recurring patterns in the last 7 days. I'd like to
>  write these changes to your config — here they are. Type `yes`
>  to apply, or `show <n>` to see the evidence for proposal N."

Do NOT auto-apply unless `DRY_RUN` is `false` explicitly AND the
user confirmed. The default for a recurring cron run should be
`DRY_RUN=true` so the system proposes but never applies silently.

### Step 6 — Apply (if confirmed)

For `allow_patterns` entries: SkillSyncer doesn't currently have a
CLI command to edit `config.yaml` directly. Write a brief instruction
file to `~/.skillsyncer/proposed-improvements.yaml` and tell the
user where to look. A future harness update may add
`skillsyncer config set` for direct application.

For missing secrets: run `skillsyncer secret-set KEY <value>`
with the value you discovered. Never ask the user to paste.

### Step 7 — Self-signature

At the end of the session, write a one-line signature to
`~/.skillsyncer/reports/improve-<timestamp>.json`:

```json
{
  "type": "improve",
  "window": "7d",
  "reports_read": 14,
  "patterns_found": 3,
  "proposals_made": 3,
  "proposals_applied": 2,
  "proposals_deferred": 1
}
```

So the next `improve` run can see what the last one did and avoid
re-proposing the same thing.

## Rules

- **Never auto-apply without consent**, even in cron mode. Cron
  should always run with `DRY_RUN=true`.
- **Cite every proposal.** If you can't point at ≥ 3 reports
  justifying a change, don't propose it.
- **Don't invent values.** Read secrets from the source they keep
  appearing in.
- **Prefer narrower patterns.** `a8f2c9d3[a-f0-9]{32}` beats
  `[a-f0-9]{40}` — wildcards are secret bypasses.
- **Don't reshape other skills' content.** This skill edits config,
  not other skills.

## Done criteria

- The loop has been closed: for every recurring pattern in WINDOW,
  there's either a proposal the user saw or a change that was
  applied with consent.
- The next run of SkillSyncer's push/pull on the same data should
  not trip the same pattern.
