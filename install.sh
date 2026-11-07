#!/usr/bin/env bash
# Install CreditsTracker on this machine.
#   macOS  → builds the menu-bar app into ~/Applications (+ `ctrack` CLI in ~/.local/bin)
#   Ubuntu → installs the GNOME Shell top-bar extension (+ `ctrack` CLI in ~/.local/bin)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$(uname -s)" in
  Darwin) exec "$ROOT/macos/build.sh" --install ;;
  Linux) ;;
  *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

command -v python3 >/dev/null || { echo "python3 is required (sudo apt install python3)" >&2; exit 1; }

UUID=creditstracker@valkeanaa.github.io
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"
mkdir -p "$DEST" "$HOME/.local/bin"
cp -r "$ROOT/gnome/$UUID/." "$DEST/"
cp "$ROOT/core/ctrack.py" "$DEST/ctrack.py"

cat > "$HOME/.local/bin/ctrack" <<EOF
#!/bin/sh
exec python3 -I "$DEST/ctrack.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/ctrack"

# First run creates ~/.config/creditstracker/config.json with auto-detected accounts (never overwrites).
python3 -I "$DEST/ctrack.py" status --cached --json >/dev/null

echo "Installed extension to $DEST and CLI to ~/.local/bin/ctrack"
if gnome-extensions info "$UUID" >/dev/null 2>&1; then
  gnome-extensions enable "$UUID"
  echo "Enabled. After an update, log out/in (Wayland) so GNOME reloads the extension code."
else
  echo "GNOME only discovers new extensions at login: log out and back in once, then run:"
  echo "  gnome-extensions enable $UUID"
fi
