#!/bin/bash
#
# Restart the app container when it stops answering, after capturing why.
#
# Every outage in the 2026-07-29..08-06 series ended when a human restarted the
# process, not when the attack stopped -- on 2026-08-05 the box spent its last
# ~10 unhealthy hours under *below-baseline* traffic.  Recovery is the slow
# part, and it depends on someone being awake.
#
# This runs on the EC2 host (not inside the container) from cron.  It probes the
# cheap /healthz endpoint that caper/middleware.py serves, so a slow page render
# never trips it: only a box that cannot answer a static string does.
#
# Why a local watchdog rather than wiring the CloudWatch alarm to SSM: the wedge
# is inside the application process, and this path has no dependency on the ALB,
# the alarm, EventBridge, IAM or an SSM agent (prod has none installed).  Fewer
# moving parts between the failure and the recovery.  The alarm stays as the
# notification channel.
#
# It captures before it restarts.  A restart destroys the only evidence of the
# second, still-undiagnosed wedge mechanism (docs/handoff-attack-mitigations.md
# §2), so an automated restart that skipped the capture would trade the
# diagnosis away for the uptime.
#
# Install:
#     sudo install -m 755 wedge-watchdog.sh /usr/local/bin/wedge-watchdog
#     sudo crontab -e
#     * * * * * /usr/local/bin/wedge-watchdog >> /var/log/wedge-watchdog.log 2>&1
#
# Verify the restart command below matches the box before installing.

set -u

HEALTH_URL="${WATCHDOG_HEALTH_URL:-http://localhost:8000/healthz}"
# Seconds to wait for a static 200.  Generous next to the ~1s a real page takes,
# so this only fires on a box that is wedged rather than busy.
TIMEOUT="${WATCHDOG_TIMEOUT:-5}"
# Consecutive failures before restarting.  With a 1-minute cron this is three
# minutes of a site that answers nothing -- long enough that a rolling deploy or
# a worker recycle cannot trigger it, short enough to beat the 30-minute alarm.
THRESHOLD="${WATCHDOG_THRESHOLD:-3}"
# Minimum gap between restarts, so a box that wedges immediately on boot gets
# restarted every 10 minutes rather than every 3, leaving room to intervene.
COOLDOWN="${WATCHDOG_COOLDOWN:-600}"

STATE_DIR="${WATCHDOG_STATE_DIR:-/var/lib/wedge-watchdog}"
FAIL_FILE="$STATE_DIR/consecutive_failures"
LAST_RESTART_FILE="$STATE_DIR/last_restart"

CONTAINER="${WATCHDOG_CONTAINER:-amplicon-prod}"
RESTART_CMD="${WATCHDOG_RESTART_CMD:-docker restart $CONTAINER}"
CAPTURE_CMD="${WATCHDOG_CAPTURE_CMD:-/usr/local/bin/wedge-capture}"

mkdir -p "$STATE_DIR"
timestamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

if curl -fsS --max-time "$TIMEOUT" -o /dev/null "$HEALTH_URL"; then
    # Only report the transition, so a healthy box logs nothing.
    if [[ -s "$FAIL_FILE" ]] && [[ "$(cat "$FAIL_FILE")" != "0" ]]; then
        echo "$(timestamp) healthy again after $(cat "$FAIL_FILE") failed probe(s)"
    fi
    echo 0 > "$FAIL_FILE"
    exit 0
fi

failures=$(( $(cat "$FAIL_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$failures" > "$FAIL_FILE"
echo "$(timestamp) health probe failed ($failures/$THRESHOLD): $HEALTH_URL"

if (( failures < THRESHOLD )); then
    exit 0
fi

now=$(date +%s)
last_restart=$(cat "$LAST_RESTART_FILE" 2>/dev/null || echo 0)
if (( now - last_restart < COOLDOWN )); then
    echo "$(timestamp) still unhealthy, but last restart was $(( now - last_restart ))s ago (cooldown ${COOLDOWN}s) -- not restarting"
    exit 0
fi

if [[ -x "$CAPTURE_CMD" ]]; then
    echo "$(timestamp) capturing worker state before restart"
    # Bounded: a capture that hangs must not stop the recovery it precedes.
    timeout 120 "$CAPTURE_CMD" || echo "$(timestamp) capture failed or timed out; restarting anyway"
else
    echo "$(timestamp) no capture tool at $CAPTURE_CMD -- restarting without evidence"
fi

echo "$(timestamp) restarting: $RESTART_CMD"
if $RESTART_CMD; then
    echo "$(timestamp) restart issued"
else
    echo "$(timestamp) RESTART FAILED -- needs a human"
fi

echo "$now" > "$LAST_RESTART_FILE"
echo 0 > "$FAIL_FILE"
