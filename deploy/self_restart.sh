#!/usr/bin/env bash
# self_restart.sh — restart the nloop service while pinging Telegram before and
# after, WITHOUT the notification dying along with the restart (ported from dtc).
#
# Why: if the agent (the nloop Telegram chat) restarts its own service from the
# inside, systemd kills the entire cgroup — including the process that was about to
# send the reply. So this script MUST be run via systemd-run (a transient unit OUTSIDE the cgroup):
#
#   systemd-run --unit=nloop-self-restart /opt/nloop/deploy/self_restart.sh
#
# Usage: self_restart.sh [service] [before_msg] [after_msg] [chat_id]
set -euo pipefail

NLOOP_DIR="/opt/nloop"
ENV_FILE="$NLOOP_DIR/.env"

SERVICE="${1:-nloop.service}"
MSG_BEFORE="${2:-hang on, restarting myself real quick 🔧}"
MSG_AFTER="${3:-hey, I'm back 👋}"
CHAT_ID="${4:-}"

TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"'')"
ALLOWED="$(grep -E '^TELEGRAM_ALLOWED_CHAT_IDS=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"'')"

if [[ -z "$CHAT_ID" ]]; then CHAT_IDS="${ALLOWED//,/ }"; else CHAT_IDS="$CHAT_ID"; fi

send() {
  local text="$1"
  [[ -z "$TOKEN" ]] && return 0
  for cid in $CHAT_IDS; do
    curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
      -d "chat_id=${cid}" --data-urlencode "text=${text}" >/dev/null || true
  done
}

send "$MSG_BEFORE"
systemctl restart "$SERVICE"

ok=0
for _ in $(seq 1 30); do
  if systemctl is-active --quiet "$SERVICE"; then ok=1; sleep 2; break; fi
  sleep 1
done

if [[ "$ok" -eq 1 ]]; then
  send "$MSG_AFTER"
else
  send "⚠️ $SERVICE didn't come back up after the restart, please check it manually."
fi
