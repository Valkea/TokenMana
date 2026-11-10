#!/usr/bin/env bash
# Install TokenMana on this machine.
#   macOS  → builds the menu-bar app into ~/Applications (+ `mana` CLI in ~/.local/bin)
#   Ubuntu → installs the GNOME Shell top-bar extension (+ `mana` CLI in ~/.local/bin)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$(uname -s)" in
  Darwin) exec "$ROOT/macos/build.sh" --install ;;
  Linux) ;;
  *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

command -v python3 >/dev/null || { echo "python3 is required (sudo apt install python3)" >&2; exit 1; }

UUID=tokenmana@valkea.github.io
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"
mkdir -p "$DEST" "$HOME/.local/bin"
cp -r "$ROOT/gnome/$UUID/." "$DEST/"
cp "$ROOT/core/mana.py" "$DEST/mana.py"
glib-compile-schemas "$DEST/schemas"

cat > "$HOME/.local/bin/mana" <<EOF
#!/bin/sh
exec python3 -I "$DEST/mana.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/mana"

# First run creates ~/.config/tokenmana/config.json with auto-detected accounts (never overwrites).
python3 -I "$DEST/mana.py" status --cached --json >/dev/null

echo "Installed extension to $DEST and CLI to ~/.local/bin/mana"
if gnome-extensions info "$UUID" >/dev/null 2>&1; then
  # extension.js re-imports a fresh copy of indicator.js on every enable: this applies the update live.
  gnome-extensions disable "$UUID" 2>/dev/null || true
  gnome-extensions enable "$UUID"
  echo "Reloaded the running extension."
  echo "(Changes to extension.js, metadata.json or the settings schema still need a log out/in.)"
else
  echo "GNOME only discovers new extensions at login: log out and back in once, then run:"
  echo "  gnome-extensions enable $UUID"
fi
