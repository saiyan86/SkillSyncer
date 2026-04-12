#!/bin/sh
# POSIX sh — works under bash, dash, zsh, ash. Don't add bashisms.
set -eu

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

# SkillSyncer is not yet on PyPI — install directly from GitHub.
# Override with: SKILLSYNCER_SOURCE=skillsyncer ./install.sh   (once published)
SOURCE="${SKILLSYNCER_SOURCE:-git+https://github.com/saiyan86/SkillSyncer.git}"

if command -v uv >/dev/null 2>&1; then
  uv tool install --force "$SOURCE"
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "$SOURCE"
elif command -v pip3 >/dev/null 2>&1; then
  pip3 install --user "$SOURCE" 2>/dev/null \
    || pip3 install --user --break-system-packages "$SOURCE"
elif command -v pip >/dev/null 2>&1; then
  pip install --user "$SOURCE" 2>/dev/null \
    || pip install --user --break-system-packages "$SOURCE"
else
  echo "ERROR: no uv / pipx / pip found. Install Python first." >&2
  exit 1
fi

echo ""
echo "✓ SkillSyncer installed"
echo ""
echo "Run:  skillsyncer init"
echo ""
