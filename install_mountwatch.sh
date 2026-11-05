#!/bin/bash
# Installs the NAS mount watchdog (launchd LaunchAgent, runs every 5 min - see
# com.clipstage.mountwatch.plist) for the CURRENT user.
#   Run once:  bash install_mountwatch.sh      Remove:  bash install_mountwatch.sh --remove
set -e
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.clipstage.mountwatch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
if [ "$1" = "--remove" ]; then
    rm -f "$PLIST"; echo "Removed $LABEL"; exit 0
fi

[ -f "$APP_DIR/.env" ] || { echo "ERROR: $APP_DIR/.env not found (copy .env.sample and fill it in first)"; exit 1; }
[ -f "$APP_DIR/mount_volumes.sh" ] || { echo "ERROR: mount_volumes.sh not found - nothing for this watchdog to run."; exit 1; }
if ! grep -q "^CLIPSTAGE_NAS_HOST=" "$APP_DIR/.env" 2>/dev/null; then
    echo "WARNING: CLIPSTAGE_NAS_HOST is not set in .env - mount_volumes.sh will skip every"
    echo "         run with nothing to do. Set CLIPSTAGE_NAS_HOST/USER/PASSWORD first."
fi

chmod +x "$APP_DIR"/*.sh
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__CLIPSTAGE_HOME__|$APP_DIR|g" "$APP_DIR/com.clipstage.mountwatch.plist" > "$PLIST"
plutil -lint "$PLIST"
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed. Runs every 5 minutes (StartInterval in the plist) while you're logged in."
echo "Test it right now (no waiting):   launchctl kickstart -k $DOMAIN/$LABEL"
echo "Then read:                        tail -f $APP_DIR/mount_watch.log"
echo "Alerts (if CLIPSTAGE_ALERT_WEBHOOK is set) only fire on a CHANGE of state, not every run."
echo "NOTE: a LaunchAgent only runs while this Mac user is logged in."
