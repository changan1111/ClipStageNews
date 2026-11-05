#!/bin/bash
#
# ClipStage v5.0 - NAS mount watchdog.
#
# mount_volumes.sh only runs when the app starts or an index runs (nightly or manual) -
# if a share drops mid-day (network blip, NAS reboot, Mac sleep/wake), nothing
# reconnects it until the next one of those. This script re-runs mount_volumes.sh on
# its own schedule (installed by install_mountwatch.sh, every 5 minutes by default) so
# a drop gets fixed automatically, usually before anyone notices.
#
# Alerting (via clipstage_alert - webhook + Mac notification), simple and literal:
#   - still/newly down this tick  -> alert EVERY tick, by design. A NAS that's down
#     for an hour means one alert every 5 minutes for that whole hour - there is no
#     throttling. If that's too noisy for your webhook, say so and it can be added
#     back as an opt-in.
#   - reconnected (even if it was only down for a single tick and this script fixed
#     it immediately) -> one "reconnected" alert, naming which share(s).
#   - nothing was wrong this tick -> silent, just a log line.
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/clipstage_env.sh" || exit 1

echo "--- $(date '+%Y-%m-%d %H:%M:%S') ---"
out="$(mount_volumes 2>&1)"
rc=$?
echo "$out"

failed="$(echo "$out" | grep ': FAILED' | sed -E 's/^ *([^:]+):.*/\1/' | paste -sd, -)"
reconnected="$(echo "$out" | grep ': RECONNECTED' | sed -E 's/^ *([^:]+):.*/\1/' | paste -sd, -)"

if [ -n "$failed" ]; then
    clipstage_alert "NAS volume(s) NOT mounted: $failed - see mount_watch.log"
elif [ -n "$reconnected" ]; then
    clipstage_alert "NAS volume(s) reconnected: $reconnected"
fi

exit "$rc"
