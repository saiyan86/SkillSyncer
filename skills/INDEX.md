# SkillSyncer fat skills — index

Eight standalone fat skills. Each is a method-call-with-parameters:
point it at a situation, pass the right arguments, get a procedure.

The **`operator/SKILL.md`** file in this repo is a thin resolver
that matches user intent to one of these. Skills never chain
automatically — the resolver picks, one runs, then the resolver
picks the next.

## The skills

| Skill | Description | Parameters |
| --- | --- | --- |
| [`skillsyncer-onboard`](skillsyncer-onboard/SKILL.md) | First-time setup: install, consent, scan, render. | — |
| [`skillsyncer-fill`](skillsyncer-fill/SKILL.md) | Primary filler on pull. Investigate before asking. | `report_path?` |
| [`skillsyncer-guard-assist`](skillsyncer-guard-assist/SKILL.md) | Resolve blocked pushes the deterministic loop couldn't auto-fix. | `report_path?` |
| [`skillsyncer-share`](skillsyncer-share/SKILL.md) | Publish local skills into a registered source repo. | `skill?`, `source?` |
| [`skillsyncer-report`](skillsyncer-report/SKILL.md) | Read a guard/fill report and present it as prose. | `type?`, `path?` |
| [`skillsyncer-status`](skillsyncer-status/SKILL.md) | Answer "what skills do I have + what shape are they in". | — |
| [`skillsyncer-investigate`](skillsyncer-investigate/SKILL.md) | Structured investigation of the user's setup (the `/investigate` pattern). | `TARGET`, `QUESTION`, `WINDOW?` |
| [`skillsyncer-improve`](skillsyncer-improve/SKILL.md) | Learning loop: read recent reports, propose config rewrites. | `WINDOW?`, `DRY_RUN?` |

## Philosophy

Every one of these skills is a fat markdown document encoding
process, judgment, and rules. They don't ship in the Python
package. They live here, in this repo, as data — exactly like
the user skills the SkillSyncer CLI is designed to manage.

The CLI doesn't need to know these exist. The agent layer loads
them on demand via `operator/SKILL.md`. That's the whole
point: thin harness, fat skills.

## Using these yourself

Your agent's operator skill (the one at `operator/SKILL.md`) is
already a thin resolver. If you have an agent (Claude Code,
Cursor, OpenClaw, Hermes, …) and you want it to be able to
manage SkillSyncer fluently, copy the eight skills into its
skill dir — or, more elegantly, register this repo as a
SkillSyncer source:

```bash
skillsyncer add https://github.com/saiyan86/SkillSyncer
skillsyncer render
```

…and SkillSyncer will hydrate them into every detected agent
dir. Your agent now knows how to onboard, fill, guard-assist,
share, report, status, investigate, and improve without you
writing any glue.
