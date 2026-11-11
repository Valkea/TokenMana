#!/usr/bin/env bash
# Install TokenMana on this machine.
#   macOS  → builds the menu-bar app into ~/Applications (+ `mana` CLI in ~/.local/bin)
#   Ubuntu → installs the GNOME Shell top-bar extension (+ `mana` CLI in ~/.local/bin)
#
# From a checkout: ./install.sh
# Without one:     curl -fsSL https://raw.githubusercontent.com/Valkea/TokenMana/main/install.sh | bash
#                  (wget -qO- … | bash on Ubuntu; TOKENMANA_VERSION=v0.2.0 picks a version, default: latest)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" && pwd)"

fetch() {  # URL → stdout. Ubuntu desktop ships wget but not curl.
  if command -v curl >/dev/null; then curl -fsSL "$1"
  elif command -v wget >/dev/null; then wget -qO- "$1"
  else echo "curl or wget is required" >&2; return 1; fi
}

# Run outside a checkout (curl … | bash): download a release, install from it, delete it.
bootstrap() {
  command -v python3 >/dev/null || { echo "python3 is required" >&2; return 1; }
  local version="${TOKENMANA_VERSION:-}" src
  if [ -z "$version" ]; then
    # The tags feed, not the API: the API's anonymous rate limit is often used up behind company proxies.
    version="$(fetch https://github.com/Valkea/TokenMana/tags.atom | python3 -c '
import re, sys
tags = re.findall(r"<title>(v\d+(?:\.\d+)*)</title>", sys.stdin.read())
print(max(tags, key=lambda t: [int(x) for x in t[1:].split(".")]))' 2>/dev/null)" \
      || { version=main; echo "Could not find the latest release: installing the development version (main)."; }
  fi
  src="$(mktemp -d)"
  trap "rm -rf '$src'" EXIT
  echo "Downloading TokenMana ${version}…"
  fetch "https://github.com/Valkea/TokenMana/archive/$version.tar.gz" | tar -xzf - -C "$src" --strip-components=1
  bash "$src/install.sh" </dev/null
}
if [ ! -f "$ROOT/core/mana.py" ]; then
  bootstrap
  exit
fi

case "$(uname -s)" in
  Darwin)
    xcode-select -p >/dev/null 2>&1 || { echo "Install the Xcode Command Line Tools first: xcode-select --install" >&2; exit 1; }
    exec "$ROOT/macos/build.sh" --install ;;
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
