#!/usr/bin/env bash


set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=$(git describe --tags --always 2>/dev/null || echo dev)

FLIGHTOPS_CONFIG="$PWD/tests/fixtures/site-config.json" python3 -m unittest discover -s tests -q
bash scripts/check-logic.sh
swift build -c release

APP=build/"FlightOps.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/collectors"
cp .build/release/FlightOps "$APP/Contents/MacOS/FlightOps"
cp collectors/*.py "$APP/Contents/Resources/collectors/"
cp assets/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>FlightOps</string>
<key>CFBundleDisplayName</key><string>FlightOps</string>
<key>CFBundleIdentifier</key><string>io.github.jammin8.flightops</string>
<key>CFBundleExecutable</key><string>FlightOps</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.2</string>
<key>CFBundleVersion</key><string>$VERSION</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>LSUIElement</key><true/>
<key>NSHumanReadableCopyright</key><string>FlightOps contributors</string>
</dict></plist>
EOF
codesign --force --deep -s - "$APP"

DEST="$HOME/Applications/FlightOps.app"

rm -rf "$HOME/Applications/Cockpit.app" "$HOME/Applications/Flight Ops.app"

rm -rf "$HOME/Applications/FlightOps.app"
pkill -x FlightOps 2>/dev/null || true
sleep 1
rm -rf "$DEST"
cp -R "$APP" "$DEST"

launchctl bootout "gui/$(id -u)/io.github.jammin8.flightops" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/io.github.jammin8.flightops.plist"
open "$DEST"
echo "Installed $DEST ($VERSION)"
