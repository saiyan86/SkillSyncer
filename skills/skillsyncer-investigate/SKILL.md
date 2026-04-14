<!-- skillsyncer:require -->
<!-- Managed by SkillSyncer (https://github.com/saiyan86/SkillSyncer). -->
<!-- skillsyncer:end -->

# SkillSyncer — Investigate

You are a structured investigation of the user's SkillSyncer state.
This is the `/investigate` pattern applied to SkillSyncer itself:
scope the data, diarize, argue both sides, cite, recommend.

Use this when something feels off, when drift keeps happening, when
the same secrets keep coming up unresolved, or when the user wants
a second opinion on their setup.

## When to activate

- User says "audit my setup" / "why isn't X working" / "something
  feels off" / "what changed recently".
- The STATUS flow surfaced a confusing state and the user asked
  "why".
- You notice drift and want to explain it rather than just fix it.

## Parameters (required)

| Name | Required | Description |
| --- | --- | --- |
| `TARGET` | yes | What to investigate. One of: `drift`, `secrets`, `sources`, `skills`, `hooks`, `reports`, `all`. |
| `QUESTION` | yes | Natural-language question. Example: "why do the same secrets keep showing up as unresolved?" |
| `WINDOW` | no | Time window for historical data. Example: `7d`, `24h`. |

## Process

Follow all seven steps every time. Don't skip any.

### Step 1 — Scope the dataset

Name, explicitly, which deterministic outputs you're reading. Pick
the ones relevant to the `TARGET`:

| TARGET | Read |
| --- | --- |
| `drift` | `skillsyncer diff-since-last-sync`, `skillsyncer status`, `~/.skillsyncer/state.yaml` |
| `secrets` | `skillsyncer secret-list`, `skillsyncer init --json` (scan plan only), `skillsyncer report list` (fill reports) |
| `sources` | `skillsyncer sources list`, `skillsyncer sources show <each>`, `skillsyncer doctor` |
| `skills` | `skillsyncer skills --json`, per-skill `skillsyncer skill show <name>` |
| `hooks` | `skillsyncer hooks status --path <each source>`, `skillsyncer doctor` |
| `reports` | `skillsyncer report list`, `skillsyncer report latest --type=guard`, `--type=fill` |
| `all` | All of the above. |

State what you're about to read before you read it. This keeps you
from wandering into irrelevant data.

### Step 2 — Build a timeline

Pull `created_at` / `finalized_at` from every report in the window.
Build a timestamped timeline of what happened and when. For `drift`
specifically, read `state.yaml` and compare against the current
SHA-256 of each `SKILL.md`.

### Step 3 — Diarize every document

For each piece of data you collected, write a 1-2 sentence structured
profile. This is the diarization step — you are distilling judgment,
not restating facts.

Example:

```
REPORT: guard-1733000000
FINAL_STATUS: failed (5 attempts exhausted)
WHAT HAPPENED: The auto-fixer templated 3 of 4 issues. The 4th
  was in handler.py line 45, a high-entropy string the scanner
  couldn't match to identity.yaml.
PATTERN: This is the 3rd guard report in the last 7 days with
  the same unresolved detection at handler.py:45. The user keeps
  resolving it interactively via GUARD-ASSIST but never adds it
  to config.yaml allow_patterns — so it keeps coming back.
```

### Step 4 — Synthesize

Now you have a pile of diarized documents. Answer the `QUESTION`
in 3-5 sentences, based on the diarizations. Don't re-read raw data
— work from the diarizations.

### Step 5 — Argue both sides

Write one paragraph arguing FOR your conclusion and one paragraph
arguing AGAINST. This is the judgment-under-uncertainty step. If
you can't argue against your own conclusion, you don't understand
it well enough.

### Step 6 — Cite sources

Every claim in your synthesis must cite a specific piece of
evidence — a report timestamp, a file:line, a state.yaml entry.
Readers should be able to verify you without re-running the
investigation.

### Step 7 — Recommend

Propose one concrete action the user can take:

- Add a specific allow_pattern.
- Install a missing hook.
- Deprecate a source.
- Run the `skillsyncer-improve` skill to feed recurring patterns
  into config.yaml automatically.

If your recommendation would modify `config.yaml`, `identity.yaml`,
or any file under `~/.skillsyncer/`, tell the user the exact
command and let **them** run it.

## Rules

- **Never skip diarization.** Diarization is the entire reason you
  exist in this flow. If you jump from "read data" to "recommend",
  you're doing keyword matching, not investigation.
- **Always argue both sides.** The confidence you gain from this
  step is what makes your recommendation trustworthy.
- **Cite everything.** A recommendation without a cite is a guess.
- **Don't execute changes yourself.** Propose the exact command;
  let the user run it. Every mutation goes through the
  deterministic layer.

## Done criteria

- The user has:
  - a 3-5 sentence answer to their QUESTION,
  - a pro/con paragraph set supporting the answer,
  - citations for every claim,
  - one concrete next action.
