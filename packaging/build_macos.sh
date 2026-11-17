#!/usr/bin/env bash
# Build dist/Resumap.app and what to hand out: dist/Resumap-<version>-macos-<arch>.dmg
# (drag to Applications) and the same app as a .zip.
# Runs on macOS, for the architecture of the Mac it runs on.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run --extra app pyinstaller --noconfirm --clean \
  --distpath dist --workpath build/pyinstaller packaging/resumap.spec

app=dist/Resumap.app
# The SDK's own claude binary is excluded in the spec; make sure it stayed out.
if find "$app" -type f -name claude -size +10M | grep -q .; then
  echo "error: claude-agent-sdk's bundled claude binary is in the app" >&2
  exit 1
fi

# Apple Silicon refuses to run code with no signature at all, so sign the
# whole bundle ad hoc. This is not a Developer ID signature: people still
# confirm the first open (README, "Install the app").
#
# Signed and zipped in a temp folder: codesign refuses Finder metadata, and a
# checkout in an iCloud-synced folder (~/Documents, ~/Desktop) gets it put
# back on the bundle as fast as it is cleared.
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
ditto "$app" "$stage/Resumap.app"
xattr -cr "$stage/Resumap.app"
codesign --force --deep --sign - "$stage/Resumap.app"
codesign --verify --deep --strict "$stage/Resumap.app"

version=$(uv run --extra app python -c 'from importlib.metadata import version; print(version("resumap"))')
zip="dist/Resumap-${version}-macos-$(uname -m).zip"
rm -f "$zip"
# ditto, not zip: it keeps the bundle's symlinks and signature intact.
ditto -c -k --keepParent "$stage/Resumap.app" "$zip"

# The disk image: the app beside a link to /Applications, so installing is one
# drag. hdiutil now and then fails with "Resource busy" on a busy machine (CI
# runners especially), so it gets three tries.
dmgsrc="$stage/dmg"
mkdir "$dmgsrc"
ditto "$stage/Resumap.app" "$dmgsrc/Resumap.app"
ln -s /Applications "$dmgsrc/Applications"
dmg="dist/Resumap-${version}-macos-$(uname -m).dmg"
rm -f "$dmg"
for try in 1 2 3; do
  if hdiutil create -volname "Resumap ${version}" -srcfolder "$dmgsrc" \
    -fs HFS+ -format UDZO -ov "$dmg" >/dev/null; then
    break
  fi
  [ "$try" = 3 ] && { echo "error: hdiutil could not create $dmg" >&2; exit 1; }
  sleep 5
done
# The signed copy replaces the unsigned one, for running it from here.
rm -rf "$app"
ditto "$stage/Resumap.app" "$app"

echo
echo "  app: $app ($(du -sh "$app" | cut -f1))"
echo "  zip: $zip ($(du -sh "$zip" | cut -f1))"
echo "  dmg: $dmg ($(du -sh "$dmg" | cut -f1))"
