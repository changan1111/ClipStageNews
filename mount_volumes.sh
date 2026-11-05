#!/bin/bash
#
# ClipStage - mount SMB shares on macOS before indexing / starting the API / editing.
#
# clipstage_env.sh's mount_volumes() calls this file if it exists (start_clipstage.sh,
# nightly_index.sh, manual_index.sh, mount_watch.sh). If it is absent those scripts assume the
# volumes are already mounted, so it is always safe to add or remove this file.
#
# Reads from .env (already loaded into the environment by clipstage_env.sh):
#
#   NAS shares (middle machine AND editor machines)
#     CLIPSTAGE_NAS_HOST       NAS hostname or IP (required)
#     CLIPSTAGE_NAS_DOMAIN     AD/Windows domain for the account (optional)
#     CLIPSTAGE_NAS_USER       username (required)
#     CLIPSTAGE_NAS_PASSWORD   password (required)
#     CLIPSTAGE_SCAN_VOLUMES   comma list of share names (default: the list clipstage_config.py scans)
#
#   Staging share (EDITOR machines only - the share the middle machine publishes)
#     CLIPSTAGE_STAGING_HOST       middle machine address (optional: unset = skip)
#     CLIPSTAGE_STAGING_USER       account on the middle machine
#     CLIPSTAGE_STAGING_PASSWORD   its password
#     CLIPSTAGE_STAGING_SHARE      share name (default: staging)
#
# Each share is mounted at /Volumes/<share name>. Already-mounted shares are left alone; this
# never unmounts anything. Safe to re-run.
#
# Security note: the password is handed to mount_smbfs through the $PASSWD environment variable
# (its documented password source), not the command line, so it does not show in `ps`.
# It still lives in plain text in .env - run `chmod 600 .env` (the installers do).

set -u

NAS_HOST="${CLIPSTAGE_NAS_HOST:-}"
NAS_DOMAIN="${CLIPSTAGE_NAS_DOMAIN:-}"
NAS_USER="${CLIPSTAGE_NAS_USER:-}"
NAS_PASSWORD="${CLIPSTAGE_NAS_PASSWORD:-}"
ST_HOST="${CLIPSTAGE_STAGING_HOST:-}"
ST_USER="${CLIPSTAGE_STAGING_USER:-}"
ST_PASSWORD="${CLIPSTAGE_STAGING_PASSWORD:-}"
ST_SHARE="${CLIPSTAGE_STAGING_SHARE:-staging}"

IFS=',' read -r -a VOLS <<< "${CLIPSTAGE_SCAN_VOLUMES:-EDIT,EDIT2,INGEST,PLAYOUT,DIGITAL,SHARE FOLDER,TRANSCODER}"

# URL-encode a piece of an smb:// URL (user/domain/share names can contain spaces, @, :, %).
_urlenc() {
    local s="$1" out="" c i
    for (( i = 0; i < ${#s}; i++ )); do
        c="${s:$i:1}"
        case "$c" in
            [a-zA-Z0-9.~_-]) out+="$c" ;;
            *) out+=$(printf '%%%02X' "'$c") ;;
        esac
    done
    printf '%s' "$out"
}

ok=0 failed=0
ERRFILE="$(mktemp /tmp/clipstage_mount_err.XXXXXX)"

# mount_one HOST DOMAIN USER PASSWORD SHARE
mount_one() {
    local host="$1" domain="$2" user="$3" password="$4" share="$5"
    local auth mountpoint enc_share mounted=0 enc_pass _
    auth="$(_urlenc "$user")"
    [ -n "$domain" ] && auth="$(_urlenc "$domain");$auth"
    share="$(echo "$share" | sed 's/^ *//;s/ *$//')"
    [ -z "$share" ] && return 0
    mountpoint="/Volumes/$share"

    if mount | grep -qF " on $mountpoint "; then
        echo "  $share: OK (already mounted)"
        ok=$((ok + 1))
        return 0
    fi

    enc_share="$(_urlenc "$share")"
    : > "$ERRFILE"
    if mkdir -p "$mountpoint" 2>/dev/null; then
        # Normal path (works when /Volumes is writable)
        if PASSWD="$password" mount_smbfs "//$auth@$host/$enc_share" "$mountpoint" 2>"$ERRFILE"; then
            mounted=1
        else
            rmdir "$mountpoint" 2>/dev/null          # remove the empty mountpoint we just created
        fi
    else
        # /Volumes is root-owned: ask the system to mount it the way Finder does
        # (open smb://; osascript segfaults under launchd) - it creates /Volumes/<share> itself.
        enc_pass="$(_urlenc "$password")"
        /usr/bin/open -g -j "smb://$auth:$enc_pass@$host/$enc_share" >/dev/null 2>"$ERRFILE"
        for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do      # wait up to ~12s for it to appear
            mount | grep -qF " on $mountpoint " && { mounted=1; break; }
            sleep 1
        done
        [ "$mounted" -eq 0 ] && echo "open smb:// did not mount it within 12s" >>"$ERRFILE"
    fi

    if [ "$mounted" -eq 1 ]; then
        echo "  $share: RECONNECTED (was missing, mounted it just now)"
        ok=$((ok + 1))
    else
        echo "  $share: FAILED - $(cat "$ERRFILE" 2>/dev/null)"
        failed=$((failed + 1))
    fi
}

did_something=0

if [ -n "$NAS_HOST" ] && [ -n "$NAS_USER" ] && [ -n "$NAS_PASSWORD" ]; then
    did_something=1
    for vol in "${VOLS[@]}"; do
        mount_one "$NAS_HOST" "$NAS_DOMAIN" "$NAS_USER" "$NAS_PASSWORD" "$vol"
    done
else
    echo "mount_volumes.sh: CLIPSTAGE_NAS_HOST / _USER / _PASSWORD not all set in .env -" \
         "skipping NAS mounts (assuming the volumes are already mounted)."
fi

if [ -n "$ST_HOST" ] && [ -n "$ST_USER" ] && [ -n "$ST_PASSWORD" ]; then
    did_something=1
    mount_one "$ST_HOST" "" "$ST_USER" "$ST_PASSWORD" "$ST_SHARE"
fi

rm -f "$ERRFILE"
[ "$did_something" -eq 0 ] && exit 0

echo "mount_volumes.sh: $ok mounted/already mounted, $failed failed"
[ "$failed" -gt 0 ] && exit 1
exit 0
