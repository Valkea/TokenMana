#!/bin/zsh
# Build TokenMana.app (menu-bar only, ad-hoc signed) and optionally install it.
#   ./build.sh            build into macos/build/
#   ./build.sh --install  build, copy to ~/Applications, install the `mana` CLI, (re)launch
set -euo pipefail
cd "${0:A:h}"

swift build -c release
APP=build/TokenMana.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$(swift build -c release --show-bin-path)/TokenMana" "$APP/Contents/MacOS/"
cp Info.plist "$APP/Contents/"
cp ../core/mana.py "$APP/Contents/Resources/"
codesign --force --sign - "$APP" >/dev/null
echo "built $APP"

if [[ "${1:-}" == "--install" ]]; then
  mkdir -p ~/Applications ~/.local/bin
  pkill -x TokenMana 2>/dev/null && sleep 0.5 || true
  rm -rf ~/Applications/TokenMana.app
  cp -R "$APP" ~/Applications/
  # CLI: `mana status`, `mana ping --idle`, `mana login <id>` …
  cat > ~/.local/bin/mana <<'EOF'
#!/bin/sh
for py in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$py" ] && exec "$py" -I "$HOME/Applications/TokenMana.app/Contents/Resources/mana.py" "$@"
done
echo "mana: python3 not found" >&2; exit 1
EOF
  chmod +x ~/.local/bin/mana
  open ~/Applications/TokenMana.app
  echo "installed ~/Applications/TokenMana.app and ~/.local/bin/mana"
fi
