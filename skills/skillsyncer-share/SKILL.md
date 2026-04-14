<!-- skillsyncer:require -->
<!-- Managed by SkillSyncer (https://github.com/saiyan86/SkillSyncer). -->
<!-- skillsyncer:end -->

# SkillSyncer — Share

You are publishing one or more local skills (from the user's agent
dirs) into a registered source repo so they flow back upstream and
become shareable with teammates.

## When to activate

- User says "share this skill" / "publish my skill" / "push it to
  our team repo" / "save this to the skills repo".
- User has just finished editing a `SKILL.md` under
  `~/.claude/skills/...` (or other agent dir) and asks what to do
  with it.

## Parameters

| Name | Required | Description |
| --- | --- | --- |
| `skill` | no | Which skill to publish. If omitted, ask. |
| `source` | no | Which registered source to publish into. Auto-pick if only one. |

## Process

### 1. Make sure there's at least one source

```
skillsyncer sources list
```

If there are zero sources, switch modes: run the `skillsyncer-onboard`
skill's "source repo" step to register one, then come back here.

### 2. Pick the source

If exactly one is registered, use it. Otherwise ask the user which
one to publish into.

### 3. Pick the skill(s)

Three ways:

- User named a specific skill → use `--skill NAME`.
- User said "all my skills" → use `--all` **but warn them first**
  that this is usually not what you want (see rules below).
- Otherwise → run the interactive picker:

```
skillsyncer publish --source <name>
```

The CLI will print a numbered list of every skill found in agent
dirs. Relay it to the user, ask which to publish ("1,3,5-7"), and
pass the answer back to stdin.

Better: list the skills via `skillsyncer skills` yourself, show the
user, and then call `skillsyncer publish --skill <each>`. This gives
you more conversational control.

### 4. Let publish run

`skillsyncer publish` will:

- Copy the selected skill dirs into the source repo
- Inject the `<!-- skillsyncer:require -->` preamble (which makes
  the repo self-installing for anyone who pulls it)
- Run a pre-flight regex scan
- If clean → `git add .` + `git commit -m "Publish N skill(s)..."`
- If dirty → refuse to commit, print the detections, exit 1

### 5. If the pre-flight scan fails

Don't try to fix it yourself. Load the `skillsyncer-guard-assist`
skill and treat the detections as unresolved items. Resolve, then
come back and re-run publish.

Or the user may prefer to clean up the skill in its **original
agent dir** first (since that's where they'll edit it next time),
then re-run publish.

### 6. Push upstream

`skillsyncer publish` pushes automatically after the commit.
The pre-push hook runs a final security scan before the push
goes out. If the push fails (e.g. no network, auth error), the
CLI will print a fallback command — surface it to the user.

## Rules

- **Default to cherry-pick, not --all.** The point of the picker
  is letting the user choose which local-state-accumulated skills
  are ready to share. `--all` is for someone who really knows
  what's in their agent dirs.
- **Let the CLI push.** `skillsyncer publish` handles the push
  automatically. Only surface the fallback command if it fails.
- **Fail loud on pre-flight detections.** If publish finds secrets,
  surface them immediately and switch into guard-assist mode. Do
  not try to auto-fix by guessing names.
- **Inject the preamble every time.** SkillSyncer does this via
  `skillsyncer publish` automatically. Don't copy skill files by
  hand — you'll forget the preamble and lose the auto-install
  mechanic for anyone who pulls downstream.

## Done criteria

- Pre-flight scan passed.
- Skills are committed and pushed to the source repo.
