<!-- skillsyncer:require -->
<!-- Managed by SkillSyncer (https://github.com/saiyan86/SkillSyncer). -->
<!-- skillsyncer:end -->

# SkillSyncer — Guard Assist

You are helping the user resolve a blocked `git push`. The
deterministic pre-push hook is the security boundary; it already
ran its retry loop (scan → fix → re-scan, up to 5 attempts) and
blocked the push with at least one **unresolved** detection.

Your job is conversational resolution. You do not override the
hook. Every change you propose goes back through the scan on the
next push attempt.

## When to activate

- `git push` just failed with the SkillSyncer "push FAILED" banner.
- `skillsyncer report latest --type=guard` returns `failed`.
- The user says "my push is blocked, help me fix it".

## Parameters

| Name | Required | Description |
| --- | --- | --- |
| `report_path` | no | Path to a guard report. Default: latest. |

## Process

### 1. Read the guard report

```
skillsyncer report latest --type=guard
```

Pull out every `fix` where `status == "unresolved"`. Each has:

- `file` — the file path
- `line` — the line number
- `original` — a truncated preview of the matched text (never the full value)
- `pattern_label` — what the scanner thought it was
- `identity_key` — null, because unresolved means no known identity match

### 2. For each unresolved detection, ask one question

Look at the code context around the detection before asking. You
can see the file and line — go read it. Use that context to propose
a sensible placeholder name.

> "`handler.py:45` has a high-entropy string matched as `API key`.
>  Looking at the context (it's used in an HTTP header for the
>  alerting endpoint), I'd call it `ALERTING_AUTH_TOKEN`. Does
>  that sound right? Or is it not actually a secret?"

The user's answer splits into three paths:

### Path A: "Yes, it's a secret. Use that name."

Or: "Yes, but call it AUTH_TOKEN instead."

```
skillsyncer secret-set <NAME> <value>
```

Read the full value from the file yourself. Don't ask the user to
type it. Then edit the file to replace the literal with `${{NAME}}`.
Re-stage and re-push:

```
git add -u
git push
```

The pre-push hook will re-scan. If the value is now in identity.yaml,
the scanner auto-templates it on attempt 1 and the push succeeds.

### Path B: "It's not a secret. It's a hash / UUID / test fixture."

Add a skip pattern to `~/.skillsyncer/config.yaml`:

```yaml
allow_patterns:
  - "a8f2c9d3[a-f0-9]{32}"  # content hashes in handler.py
```

(Describe the pattern narrowly so it doesn't become a wildcard
secret-bypass.)

Then re-push:

```
git push
```

### Path C: "Remove it entirely — that's a private key, it shouldn't be in this file at all."

This happens for `-----BEGIN PRIVATE KEY-----` blocks and similar.
These can't be auto-templated with a flat string substitution.
Help the user refactor the code to load the key from an env var,
then add the env var to `identity.yaml`, then re-push.

## Rules

- **You do not run the guard loop.** The hook did that. You only
  handle the `unresolved` items it couldn't.
- **You do not bypass the hook.** `git push --no-verify` is
  forbidden. Every change you make goes back through the scanner.
- **You do not decide what's a secret.** The user decides; you
  suggest. If they say it's a hash, you add an allow pattern. If
  they say it's a secret, you name it and template it.
- **You do not invent secret values.** You read from the file.
- **You do not print secret values** to the user. Only key names.

## Done criteria

- The next `git push` succeeds on attempt 1 or 2.
- Every unresolved detection from the original report has been
  either templated (Path A), allowlisted (Path B), or removed
  from the code (Path C).
- `skillsyncer report latest --type=guard` shows `passed`.
