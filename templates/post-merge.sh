#!/bin/bash
# [skillsyncer:hook] — do not edit this section
CHANGED=$(skillsyncer diff-since-last-sync 2>&1 || true)
if [ -n "$CHANGED" ]; then
  mkdir -p "$HOME/.skillsyncer/reports"
  REPORT_FILE="$HOME/.skillsyncer/reports/fill-$(date +%s).json"
  skillsyncer fill --auto --report="$REPORT_FILE" >&2 || true
  skillsyncer render --report="$REPORT_FILE" >&2 || true
  STATUS=$(skillsyncer report status "$REPORT_FILE" 2>&1 || echo "unknown")
  if [ "$STATUS" = "partial" ]; then
    echo "[SkillSyncer] Some skills need credentials. Your agent will help." >&2
  else
    echo "[SkillSyncer] ✓ All skills rendered." >&2
  fi
fi
# [/skillsyncer:hook]
