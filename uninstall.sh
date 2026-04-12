#!/bin/sh
# POSIX sh — works under bash, dash, zsh, ash. Don't add bashisms.
set -eu

# SkillSyncer uninstaller.
#
# Default: removes the `skillsyncer` binary only. Your secrets,
# rendered skills, and git hooks are kept (the hooks become a
# silent no-op without the binary on PATH).
#
# Flags:
#   --purge        also delete ~/.skillsyncer/ (secrets, config, state, repos)
#   --yes / -y     don't prompt for purge confirmation
#   --help / -h    show usage and exit
#
# Examples:
#   curl -fsSL .../uninstall.sh | sh
#   curl -fsSL .../uninstall.sh | sh -s -- --purge
#   curl -fsSL .../uninstall.sh | sh -s -- --purge --yes

PURGE=0
YES=0

usage() {
  cat <<'USAGE'
Usage: uninstall.sh [--purge] [--yes]

  --purge        After removing the binary, delete ~/.skillsyncer/
                 entirely (secrets, config, state, cloned repos).
  --yes, -y      Don't prompt before purging. Required when --purge
                 is used in a non-interactive shell (e.g. piped from
                 curl without a TTY).
  --help, -h     Show this help and exit.

Default behavior (no flags):
  - Removes the skillsyncer binary via uv tool / pipx / pip.
  - KEEPS ~/.skillsyncer/ (your secrets and config).
  - KEEPS rendered skills in agent dirs.
  - KEEPS git hooks in repos (they self-disable when the binary is gone).
USAGE
}

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes|-y) YES=1 ;;
    --help|-h) usage; exit 0 ;;
    *)
      echo "ERROR: unknown argument: $arg" >&2
      echo "Try: uninstall.sh --help" >&2
      exit 2
      ;;
  esac
done

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

if [ "$PURGE" = "1" ]; then
  if [ ! -d "$HOME/.skillsyncer" ]; then
    echo ""
    echo "[OK] No ~/.skillsyncer/ to purge."
    exit 0
  fi

  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo " --purge will PERMANENTLY delete the following:"
  echo "═══════════════════════════════════════════════════════════════"
  echo ""
  echo "   ~/.skillsyncer/identity.yaml      (your stored secrets)"
  echo "   ~/.skillsyncer/config.yaml        (your sources / targets)"
  echo "   ~/.skillsyncer/state.yaml         (sync state)"
  echo "   ~/.skillsyncer/reports/           (run reports)"
  echo "   ~/.skillsyncer/repos/             (cloned source repos)"
  echo ""
  echo " Rendered skills in ~/.claude/skills/, ~/.cursor/skills/, etc."
  echo " are NOT touched. Hooks in your project repos are NOT touched."
  echo ""

  if [ "$YES" = "1" ]; then
    CONFIRM="PURGE"
  elif [ -e /dev/tty ]; then
    printf " Type 'PURGE' to confirm (anything else cancels): " >/dev/tty
    read -r CONFIRM </dev/tty || CONFIRM=""
  else
    echo " ERROR: Refusing to purge in non-interactive shell without --yes." >&2
    echo " Re-run:   curl -fsSL .../uninstall.sh | sh -s -- --purge --yes" >&2
    exit 1
  fi

  if [ "$CONFIRM" = "PURGE" ]; then
    rm -rf "$HOME/.skillsyncer"
    echo ""
    echo "[OK] ~/.skillsyncer/ purged."
  else
    echo ""
    echo "Cancelled. ~/.skillsyncer/ was not touched."
    exit 1
  fi
  exit 0
fi

echo ""
echo "Your data is intact:"
echo "  ~/.skillsyncer/    secrets, config, sync state"
echo "  ~/.claude/skills/, ~/.cursor/skills/, ...   rendered skills"
echo "  .git/hooks/        unchanged (silent no-op without the binary)"
echo ""
echo "To wipe SkillSyncer data too:"
echo "  rm -rf ~/.skillsyncer"
echo ""
echo "Or re-run with --purge:"
echo "  curl -fsSL https://raw.githubusercontent.com/saiyan86/SkillSyncer/main/uninstall.sh | sh -s -- --purge"
echo ""
