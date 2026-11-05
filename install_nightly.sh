#!/bin/bash
# Installs the scheduled index job for the CURRENT user (launchd LaunchAgent).
# Run once:  bash install_nightly.sh      Remove:  bash install_nightly.sh --remove
set -e
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.clipstage.indexer"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
if [ "$1" = "--remove" ]; then
    rm -f "$PLIST"; echo "Removed $LABEL"; exit 0
fi

[ -f "$APP_DIR/.env" ] || { echo "ERROR: $APP_DIR/.env not found (copy .env.sample and fill it in first)"; exit 1; }
chmod +x "$APP_DIR"/*.sh
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__CLIPSTAGE_HOME__|$APP_DIR|g" "$APP_DIR/com.clipstage.indexer.plist" > "$PLIST"
plutil -lint "$PLIST"
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed. It runs daily at the time set in the plist (default 17:00)."
echo "Test it right now (no waiting):   launchctl kickstart -k $DOMAIN/$LABEL"
echo "Then read:                        tail -f $APP_DIR/indexer.log"
echo "NOTE: a LaunchAgent only runs while this Mac user is logged in."
