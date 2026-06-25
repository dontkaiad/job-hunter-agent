#!/usr/bin/env bash
# monitor.sh — host-side liveness watchdog for the job-hunter stack.
#
# Run via cron every 10 minutes, e.g.:
#   */10 * * * * /opt/job-hunter/monitor.sh >> /var/log/job-hunter-monitor.log 2>&1
#
# Sends alerts to the Telegram ops channel using the same env vars as tg_logger.py:
#   TG_LOG_BOT_TOKEN         — ops bot token
#   TG_LOG_CHAT_ID           — ops supergroup id
#   TG_LOG_THREAD_JOBHUNTER  — ops topic thread id (optional)
#
# Checks performed:
#   1. Container health  (job-hunter, dashboard)
#   2. Disk / RAM        (host resources)
#   3. Qdrant healthz    (vector store)
#   4. Harvest staleness (ops_heartbeat table — this file adds this check)
#
# Harvest staleness dedup: /tmp/harvest_stale_alerted flag file.
#   SET   when the alert fires (harvest too old).
#   CLEAR when harvest recovers (and a "recovered" notice is sent).
#   → ONE alert per incident; no spam every 10 min.
#
# Harvest stale threshold: 26 hours.
#   Harvest runs daily at 10:00 local. 26 h = 24 h cycle + 2 h buffer so a
#   natural brief delay (slow network, slight job drift) doesn't false-alarm,
#   but a missed day (like today's DB-restart incident) is caught by ~12:00.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Load secrets from host .env (not in the image; adjust path for your deploy).
ENV_FILE="${ENV_FILE:-$(dirname "$0")/.env}"
# shellcheck source=/dev/null
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE"

HARVEST_STALE_HOURS=26        # alert threshold (see header for rationale)
HARVEST_STALE_FLAG="/tmp/harvest_stale_alerted"

# ---------------------------------------------------------------------------
# Helper: send a message to the Telegram ops channel
# ---------------------------------------------------------------------------
# Uses the same TG_LOG_* env vars as tg_logger.py so this script and the app
# post to the same thread. Python is used for JSON serialisation (avoids jq
# dependency and handles Unicode/special chars in the message safely).

_tg_send() {
    local msg="$1"
    [[ -z "${TG_LOG_BOT_TOKEN:-}" || -z "${TG_LOG_CHAT_ID:-}" ]] && return 0
    python3 - "$msg" <<'PYEOF'
import json, os, sys, urllib.request

msg = sys.argv[1]
payload = {
    "chat_id": os.environ["TG_LOG_CHAT_ID"],
    "text": msg,
    "disable_web_page_preview": True,
}
thr = os.environ.get("TG_LOG_THREAD_JOBHUNTER", "").strip()
if thr:
    payload["message_thread_id"] = int(thr)

req = urllib.request.Request(
    f"https://api.telegram.org/bot{os.environ['TG_LOG_BOT_TOKEN']}/sendMessage",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    urllib.request.urlopen(req, timeout=10)
except Exception as exc:
    print(f"monitor.sh: _tg_send failed: {exc}", file=sys.stderr)
PYEOF
}

# ---------------------------------------------------------------------------
# Check 4: Harvest staleness
# ---------------------------------------------------------------------------
# Reads ops_heartbeat.last_at directly from Postgres via psql — no job-hunter
# process needed. Works even when the container is dead (pure host-side check).

check_harvest_staleness() {
    if [[ -z "${DATABASE_URL:-}" ]]; then
        echo "[monitor] DATABASE_URL not set — skipping harvest staleness check" >&2
        return 0
    fi

    # Query: age of the last successful harvest in hours.
    # last_at is stored as UTC ISO-8601 TEXT; Postgres casts it to timestamptz.
    # Returns empty string if no row exists (fresh deploy / table missing).
    local age_hours
    age_hours=$(
        psql "$DATABASE_URL" -t -A -c \
            "SELECT ROUND(EXTRACT(EPOCH FROM (NOW() - last_at::timestamptz)) / 3600, 1)
             FROM ops_heartbeat WHERE name = 'harvest'" \
            2>/dev/null || true
    )
    age_hours="${age_hours//[[:space:]]/}"  # trim whitespace

    if [[ -z "$age_hours" ]]; then
        # No heartbeat row yet (fresh deploy) OR psql couldn't connect.
        # Do not alert; clear any stale flag so a deploy doesn't leave it set.
        rm -f "$HARVEST_STALE_FLAG"
        echo "[monitor] harvest_staleness: no heartbeat row (fresh deploy or psql error) — skipped"
        return 0
    fi

    echo "[monitor] harvest_staleness: last_at ${age_hours}h ago (threshold ${HARVEST_STALE_HOURS}h)"

    # Float comparison via awk (bc also works; awk is always available).
    local is_stale
    is_stale=$(awk -v age="$age_hours" -v thr="$HARVEST_STALE_HOURS" \
                   'BEGIN { print (age + 0 > thr + 0) ? 1 : 0 }')

    if [[ "$is_stale" == "1" ]]; then
        if [[ ! -f "$HARVEST_STALE_FLAG" ]]; then
            # First detection — fetch the raw timestamp for the alert message.
            local last_at
            last_at=$(
                psql "$DATABASE_URL" -t -A -c \
                    "SELECT last_at FROM ops_heartbeat WHERE name = 'harvest'" \
                    2>/dev/null || echo "unknown"
            )
            last_at="${last_at//[[:space:]]/}"
            _tg_send "⚠️ jobhunter: harvest stale — last run ${last_at} (${age_hours}h ago). DB restart or crash? Check container logs."
            touch "$HARVEST_STALE_FLAG"
            echo "[monitor] harvest_staleness: ALERT sent (flag set)"
        else
            echo "[monitor] harvest_staleness: still stale, flag already set — no repeat alert"
        fi
    else
        # Harvest is fresh.
        if [[ -f "$HARVEST_STALE_FLAG" ]]; then
            _tg_send "✅ jobhunter: harvest recovered — last run ${age_hours}h ago"
            rm -f "$HARVEST_STALE_FLAG"
            echo "[monitor] harvest_staleness: recovered, flag cleared"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Checks 1–3: containers / disk / RAM / Qdrant
# (existing monitor.sh body goes here — add check_harvest_staleness below it)
# ---------------------------------------------------------------------------

# check_containers   # your existing function
# check_disk_ram     # your existing function
# check_qdrant       # your existing function

# ---------------------------------------------------------------------------
# Check 4 (new): harvest staleness
# ---------------------------------------------------------------------------

check_harvest_staleness
