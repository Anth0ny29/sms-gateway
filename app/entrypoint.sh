#!/bin/bash
set -e

DEVICE="${DEVICE:-/dev/mobile}"
CONNECTION="${CONNECTION:-at}"
PIN="${PIN:-}"
WEBHOOK_URL="${WEBHOOK_URL:-}"
API_USER="${API_USER:-admin}"
API_PASS="${API_PASS:-admin}"
POLL_INTERVAL="${POLL_INTERVAL:-3}"
SIGNAL_REFRESH="${SIGNAL_REFRESH:-60}"

# Strip quotes from PIN
if [ -n "$PIN" ]; then
    PIN=$(echo "$PIN" | tr -d '"' | tr -d "'")
fi

echo "========================================="
echo "  Gammu SMS Gateway v2 - Starting..."
echo "  Mode: python-gammu direct (no daemon)"
echo "========================================="
echo "  Device:      $DEVICE"
echo "  Connection:  $CONNECTION"
echo "  Webhook URL: ${WEBHOOK_URL:-<not set>}"
echo "  API auth:    $API_USER / ****"
echo "  Poll:        ${POLL_INTERVAL}s"
echo "========================================="

# ─── Generate gammurc ───
cat > /etc/gammurc <<EOF
[gammu]
device = ${DEVICE}
connection = ${CONNECTION}
logformat = textalldate
EOF

# ─── Enter PIN if needed ───
if [ -n "$PIN" ]; then
    echo "Entering PIN..."
    gammu --config /etc/gammurc entersecuritycode PIN "$PIN" 2>/dev/null || true
fi

# ─── Create directories ───
mkdir -p /var/spool/gammu/received
mkdir -p /var/log/gammu

# ─── Export settings for the Python app ───
export GAMMU_CONFIG="/etc/gammurc"
export WEBHOOK_URL
export API_USER
export API_PASS
export POLL_INTERVAL
export SIGNAL_REFRESH

echo "Starting API server..."
exec uvicorn main:app --host 0.0.0.0 --port 5000 --log-level info
