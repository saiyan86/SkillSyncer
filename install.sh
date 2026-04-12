#!/bin/bash
set -euo pipefail

VERSION="0.1.0"
INSTALL_DIR="$HOME/.local/bin"

echo "Installing SkillSyncer v$VERSION..."

mkdir -p "$INSTALL_DIR"
case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *)
    {
      echo ""
      echo "# Added by SkillSyncer installer"
      echo "export PATH=\"$INSTALL_DIR:\$PATH\""
    } >> "$HOME/.bashrc"
    if [ -f "$HOME/.zshrc" ]; then
      {
        echo ""
        echo "# Added by SkillSyncer installer"
        echo "export PATH=\"$INSTALL_DIR:\$PATH\""
      } >> "$HOME/.zshrc"
    fi
    export PATH="$INSTALL_DIR:$PATH"
    ;;
esac

if command -v pipx >/dev/null 2>&1; then
  pipx install skillsyncer
elif command -v pip >/dev/null 2>&1; then
  pip install --user skillsyncer 2>/dev/null \
    || pip install --user --break-system-packages skillsyncer
elif command -v pip3 >/dev/null 2>&1; then
  pip3 install --user skillsyncer 2>/dev/null \
    || pip3 install --user --break-system-packages skillsyncer
else
  echo "ERROR: no pip / pipx found. Install Python first." >&2
  exit 1
fi

echo ""
echo "✓ SkillSyncer installed"
echo ""
echo "Run:  skillsyncer init"
echo ""
