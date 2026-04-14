<!-- skillsyncer:require -->
<!-- Managed by SkillSyncer (https://github.com/saiyan86/SkillSyncer). -->
<!-- skillsyncer:end -->

# SkillSyncer — Onboard

You are running the first-time SkillSyncer setup for this user.
Your job is to get them from **zero** to **working skills** with a
single command, stepping in only if it fails.

## When to activate

- The user typed `skillsyncer onboard` or `skillsyncer init` and
  you're the assistant attached to that shell.
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

### 2. Run the interactive wizard

As soon as the binary is confirmed present, run:

```
skillsyncer onboard
```

This is a fully interactive wizard that handles everything in one
pass — no follow-up commands needed:

- **Step 1 — Scan:** probes the environment for agents, skills, and
  credentials. Shows a consent screen before reading any credential
  values. The user answers Y/n once.
- **Step 2 — Source repo:** offers to create a new private GitHub
  repo (`gh repo create`), connect an existing one (paste URL), or
  skip. The user chooses once.
- **Step 3 — Render:** hydrates `${{...}}` placeholders into every
  detected agent dir.

Let the wizard run to completion. Do not try to drive the individual
sub-steps yourself — `skillsyncer onboard` handles them.

### 3. Hand off to FILL if anything is still missing

If the wizard exits with a non-zero code or reports unfilled
placeholders, load the `skillsyncer-fill` skill and run its process.

### Fallback: headless / agent-driven path

If the shell is non-interactive (no TTY) or the user explicitly
wants the agent to drive each step, use the lower-level commands:

1. `skillsyncer init --json` — get the scan plan (no cred read)
2. Show plan, ask consent, then `skillsyncer init --yes` or
   `skillsyncer init --no-scan`
3. `skillsyncer add <url>` — register source repo if desired
4. `skillsyncer render` — hydrate placeholders

## Rules

- **Let the wizard run.** Don't pre-empt `skillsyncer onboard` with
  individual sub-commands unless the wizard isn't available.
- **Never read credential files yourself before consent.** The wizard
  shows a consent screen; the fallback path uses `init --json` first.
- **Never echo credential values back to the user.** Only key names.
- **Don't write `identity.yaml` directly.** Use
  `skillsyncer secret-set KEY VALUE`.

## Done criteria

- `skillsyncer status` shows at least one target and no error.
- `~/.skillsyncer/config.yaml` exists.
- `~/.skillsyncer/identity.yaml` exists.
- All `${{...}}` placeholders in loaded skills are either filled or
  explicitly deferred (user said "I'll do it later").
