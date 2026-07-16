#!/usr/bin/env bash
# self_restart.sh — restart service nloop sambil kasih kabar di Telegram
# sebelum/sesudah, TANPA notifikasinya ikut mati bareng restart (port pola dtc).
#
# Kenapa: kalau agent (chat Telegram nloop) restart service-nya sendiri dari
# dalam, systemd matiin seluruh cgroup — termasuk proses yang mau ngirim balasan.
# Jadi script ini HARUS dijalankan via systemd-run (transient unit di LUAR cgroup):
#
#   systemd-run --unit=nloop-self-restart /opt/nloop/deploy/self_restart.sh
#
# Usage: self_restart.sh [service] [before_msg] [after_msg] [chat_id]
set -euo pipefail

NLOOP_DIR="/opt/nloop"
ENV_FILE="$NLOOP_DIR/.env"

SERVICE="${1:-nloop.service}"
MSG_BEFORE="${2:-bentar yah gue restart dulu 🔧}"
MSG_AFTER="${3:-hi gue balik lagi 👋}"
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
  send "⚠️ $SERVICE gagal balik aktif setelah restart, tolong dicek manual."
fi
