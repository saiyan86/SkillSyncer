#!/bin/bash
# [skillsyncer:hook] — do not edit this section
set -euo pipefail

# If SkillSyncer was uninstalled, this hook becomes a silent no-op
# so it never blocks the user's push. Reinstall to re-arm:
#   curl -fsSL https://raw.githubusercontent.com/saiyan86/SkillSyncer/main/install.sh | sh
command -v skillsyncer >/dev/null 2>&1 || exit 0

MAX_RETRIES=5
ATTEMPT=0
mkdir -p "$HOME/.skillsyncer/reports"
REPORT_FILE="$HOME/.skillsyncer/reports/guard-$(date +%s).json"
skillsyncer report create --type=guard --path="$REPORT_FILE" >/dev/null

while [ $ATTEMPT -lt $MAX_RETRIES ]; do
  ATTEMPT=$((ATTEMPT + 1))

  if skillsyncer scan --staged --format=json >/tmp/skillsyncer-scan.json 2>&1; then
    skillsyncer report finalize "$REPORT_FILE" --status=passed >/dev/null
    echo "[SkillSyncer] ✓ Push clean (attempt $ATTEMPT)" >&2
    break
  fi

  ISSUES=$(cat /tmp/skillsyncer-scan.json)
  skillsyncer report update "$REPORT_FILE" --attempt="$ATTEMPT" --issues="$ISSUES" >/dev/null
  echo "[SkillSyncer] Attempt $ATTEMPT/$MAX_RETRIES — auto-fixing..." >&2
  skillsyncer guard --fix --report="$REPORT_FILE" >&2 || true
  git add -u
done

if [ $ATTEMPT -ge $MAX_RETRIES ]; then
  if ! skillsyncer scan --staged --format=json >/dev/null 2>&1; then
    skillsyncer report finalize "$REPORT_FILE" --status=failed >/dev/null
    echo "" >&2
    echo "══════════════════════════════════════════════════════" >&2
    echo " SkillSyncer: push FAILED after $MAX_RETRIES attempts" >&2
    echo " Report: $REPORT_FILE" >&2
    echo "══════════════════════════════════════════════════════" >&2
    exit 1
  fi
fi
# [/skillsyncer:hook]
