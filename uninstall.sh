#!/bin/sh
# POSIX sh — works under bash, dash, zsh, ash. Don't add bashisms.
set -eu

# SkillSyncer uninstaller.
#
# This script removes the `skillsyncer` binary only. It does NOT
# touch your secrets, your skills, or any git hooks in your repos:
#
#   ~/.skillsyncer/identity.yaml   (your secrets)         KEPT
#   ~/.skillsyncer/config.yaml     (your sources/targets) KEPT
#   ~/.skillsyncer/state.yaml      (sync state)           KEPT
#   ~/.skillsyncer/reports/        (transient reports)    KEPT
#   ~/.claude/skills/, ~/.cursor/skills/, etc.            KEPT
#   .git/hooks/pre-push, post-merge in your repos         KEPT (no-op
#                                                          without the
#                                                          skillsyncer
#                                                          binary)
#
# To wipe SkillSyncer's data too, after this script runs:
#   rm -rf ~/.skillsyncer

echo "Uninstalling SkillSyncer..."

uninstalled=0

if command -v uv >/dev/null 2>&1; then
  if uv tool uninstall skillsyncer >/dev/null 2>&1; then
    uninstalled=1
  fi
fi

if [ "$uninstalled" = "0" ] && command -v pipx >/dev/null 2>&1; then
  if pipx uninstall skillsyncer >/dev/null 2>&1; then
    uninstalled=1
  fi
fi

if [ "$uninstalled" = "0" ] && command -v pip3 >/dev/null 2>&1; then
  if pip3 uninstall -y skillsyncer >/dev/null 2>&1; then
    uninstalled=1
  fi
fi

if [ "$uninstalled" = "0" ] && command -v pip >/dev/null 2>&1; then
  if pip uninstall -y skillsyncer >/dev/null 2>&1; then
    uninstalled=1
  fi
fi

if [ "$uninstalled" = "0" ]; then
  echo "ERROR: could not find skillsyncer in uv / pipx / pip." >&2
  echo "If you installed it some other way, remove it manually." >&2
  exit 1
fi

echo ""
echo "[OK] SkillSyncer binary removed."
echo ""
echo "Your data is intact:"
echo "  ~/.skillsyncer/    secrets, config, sync state"
echo "  ~/.claude/skills/, ~/.cursor/skills/, ...   rendered skills"
echo "  .git/hooks/        unchanged (silent no-op without the binary)"
echo ""
echo "To wipe SkillSyncer data too: rm -rf ~/.skillsyncer"
echo ""
