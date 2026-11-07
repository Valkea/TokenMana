#!/bin/zsh
# Build CreditsTracker.app (menu-bar only, ad-hoc signed) and optionally install it.
#   ./build.sh            build into macos/build/
#   ./build.sh --install  build, copy to ~/Applications, install the `ctrack` CLI, (re)launch
set -euo pipefail
cd "${0:A:h}"

swift build -c release
APP=build/CreditsTracker.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$(swift build -c release --show-bin-path)/CreditsTracker" "$APP/Contents/MacOS/"
cp Info.plist "$APP/Contents/"
cp ../core/ctrack.py "$APP/Contents/Resources/"
codesign --force --sign - "$APP" >/dev/null
echo "built $APP"

if [[ "${1:-}" == "--install" ]]; then
  mkdir -p ~/Applications ~/.local/bin
  pkill -x CreditsTracker 2>/dev/null && sleep 0.5 || true
  rm -rf ~/Applications/CreditsTracker.app
  cp -R "$APP" ~/Applications/
  # CLI: `ctrack status`, `ctrack ping --idle`, `ctrack login <id>` …
  cat > ~/.local/bin/ctrack <<'EOF'
#!/bin/sh
for py in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$py" ] && exec "$py" -I "$HOME/Applications/CreditsTracker.app/Contents/Resources/ctrack.py" "$@"
done
echo "ctrack: python3 not found" >&2; exit 1
EOF
  chmod +x ~/.local/bin/ctrack
  open ~/Applications/CreditsTracker.app
  echo "installed ~/Applications/CreditsTracker.app and ~/.local/bin/ctrack"
fi
