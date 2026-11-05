#!/bin/bash
# Runs the ClipStage API in the background (launchd LaunchAgent) - survives closing Terminal.
#   Install / update:  bash install_api_service.sh
#   Remove:            bash install_api_service.sh --remove
#   Restart:           launchctl kickstart -k gui/$(id -u)/com.clipstage.api
set -e
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.clipstage.api"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
if [ "$1" = "--remove" ]; then
    rm -f "$PLIST"; echo "Removed $LABEL"; exit 0
fi

[ -f "$APP_DIR/.env" ] || { echo "ERROR: $APP_DIR/.env not found"; exit 1; }
chmod +x "$APP_DIR"/*.sh
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__CLIPSTAGE_HOME__|$APP_DIR|g" "$APP_DIR/com.clipstage.api.plist" > "$PLIST"
plutil -lint "$PLIST"

# Stop any copy started by hand in a terminal, so the port is free.
pkill -f "uvicorn api:app" 2>/dev/null || true
sleep 1

launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed. ClipStage API now starts at login and restarts if it crashes."
echo "Logs:    tail -f $APP_DIR/api.log"
echo "Restart: launchctl kickstart -k $DOMAIN/$LABEL"
echo "NOTE: runs only while this Mac user is logged in."
