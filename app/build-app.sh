#!/bin/sh
# Builds "Golem.app", a menu bar app with no Dock icon, and prints its path.
set -eu
cd "$(dirname "$0")"
cargo build --release
app="target/release/Golem.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp target/release/golem-board-app "$app/Contents/MacOS/golem-board-app"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>golem-board-app</string>
  <key>CFBundleIdentifier</key><string>golem.board</string>
  <key>CFBundleName</key><string>Golem</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>Golem opens a terminal window to pick up tasks.</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
codesign --force --sign - "$app"
echo "$app"
