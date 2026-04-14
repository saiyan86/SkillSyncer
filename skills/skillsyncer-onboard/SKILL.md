<!-- skillsyncer:require -->
<!-- Managed by SkillSyncer (https://github.com/saiyan86/SkillSyncer). -->
<!-- skillsyncer:end -->

# SkillSyncer — Onboard

You are running the first-time SkillSyncer setup for this user.
Your job is to get them from **zero** to **working skills** in a
single conversation, asking one consent and one optional question.

## When to activate

- The user typed `skillsyncer init` and you're the assistant attached
  to that shell.
- The user loaded a skill file that has a `<!-- skillsyncer:require -->`
  preamble and you notice `skillsyncer` is not on PATH.
- The user said "set up skillsyncer" / "install the thing from the
  preamble" / "onboard me" / anything equivalent.

## Process

### 1. Install the binary (if not already installed)

```
curl -fsSL https://raw.githubusercontent.com/saiyan86/SkillSyncer/main/install.sh | sh
```

On Windows (PowerShell):

```
iwr -useb https://raw.githubusercontent.com/saiyan86/SkillSyncer/main/install.ps1 | iex
```

Verify: `skillsyncer --version` exits 0.

### 2. Get the scan plan WITHOUT reading any credentials

```
skillsyncer init --json
```

This returns a proposal including:

- `agents` — detected agent skill directories
- `existing_skills` — SKILL.md files already on disk
- `credential_scan_plan` — the list of files SkillSyncer *would* read
- `credentials` — **empty** (scan hasn't run yet)
- `git` — gh auth state

You are responsible for asking the user for consent BEFORE SkillSyncer
reads any of those files. The CLI's own `init` has a consent prompt
but the agent path uses `--json` precisely so you own this step.

### 3. Present the scan plan

Show the user the locations grouped by kind:

- Project files (`./.env*`, `./docker-compose.yml`)
- User home (`~/.env*`, `~/.kube/config`)
- Shell environment (matched credential-shaped vars)
- AI tool config dirs (`~/.claude`, `~/.cursor`, `~/.openclaw`, etc.)

Tell them plainly: **"Values stay on this machine. Only key names are
ever printed. OK to scan?"**

### 4. If they say yes, run the actual scan

```
skillsyncer init --yes
```

`--yes` skips the CLI's own consent prompt since you already handled it.

### 5. If they say no

```
skillsyncer init --no-scan
```

Still creates `~/.skillsyncer/` with an empty `identity.yaml`. Explain
they can add secrets by hand later with `skillsyncer secret-set`.

### 6. Present the discovery summary

After the scan runs, show:

- Which agents were detected
- How many existing skills were found (grouped by agent)
- Which credentials were discovered (KEY NAMES only — never values)
- Which credentials are needed but not yet found

### 7. Ask about a source repo (one question, three choices)

> "Do you want to sync your skills across machines?
>  A) Create a new private GitHub repo for me (`gh repo create`)
>  B) Use an existing repo — paste the URL
>  C) Skip for now — local only"

If A and `gh` is authenticated (you saw it in the discovery output),
run `gh repo create <name> --private` then `skillsyncer add <url>`.

If B, run `skillsyncer add <url>`.

If C, do nothing. Tell them they can run `skillsyncer add` later.

### 8. Render

```
skillsyncer render
```

Fills every `${{...}}` placeholder SkillSyncer can resolve.

### 9. Hand off to FILL if anything is still missing

If `skillsyncer render` exits non-zero, some skills still need
credentials. Load the `skillsyncer-fill` skill and run its process.

## Rules

- **Never read credential files yourself before consent.** Always go
  through `skillsyncer init --json` first (plan only), then
  `skillsyncer init --yes` (scan).
- **Never echo credential values back to the user.** Not in summaries,
  not in confirmations. Only key names.
- **Don't write `identity.yaml` directly.** Always use
  `skillsyncer secret-set KEY VALUE`.
- **Don't bypass the consent prompt in scripted mode either.** If
  stdin isn't a TTY, default to `--no-scan` and tell the user how
  to re-run with `--yes`.

## Done criteria

- `skillsyncer status` shows at least one target and no error.
- `~/.skillsyncer/config.yaml` exists.
- `~/.skillsyncer/identity.yaml` exists.
- All `${{...}}` placeholders in loaded skills are either filled or
  explicitly deferred (user said "I'll do it later").
