#!/usr/bin/env bash
# Bump the version everywhere it is written, commit and tag. Pushing stays manual.
#   scripts/release.sh 0.2.6
#   git push origin main v0.2.6
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
NEW="${1:?usage: scripts/release.sh X.Y.Z}"
[[ "$NEW" =~ ^[0-9]+(\.[0-9]+)+$ ]] || { echo "not a version: $NEW (no leading v)" >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "commit or stash your tracked changes first" >&2; exit 1; }
git rev-parse -q --verify "refs/tags/v$NEW" >/dev/null && { echo "tag v$NEW already exists" >&2; exit 1; }
OLD="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' core/tokenmana/constants.py)"
[ -n "$OLD" ] || { echo "VERSION not found in core/tokenmana/constants.py" >&2; exit 1; }

FILES=(core/tokenmana/constants.py macos/Info.plist gnome/tokenmana@valkea.github.io/metadata.json install.sh README.md)
OLD_RE="${OLD//./\\.}"
for f in "${FILES[@]}"; do
  sed -i.bak -E "s/(^|[^0-9.])${OLD_RE}([^0-9.]|$)/\1${NEW}\2/g" "$f"   # whole versions only: 0.2.4, v0.2.4
  rm -f "$f.bak"
  grep -q "$NEW" "$f" || { echo "no version found in $f" >&2; git checkout -- "${FILES[@]}"; exit 1; }
done
git diff --stat
git commit -qam "chore: bump version to $NEW"
git tag "v$NEW"
echo "Committed and tagged v$NEW. Publish with: git push origin main v$NEW"
