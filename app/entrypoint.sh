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
commtimeout = 10
synchronizetime = no
EOF

# ─── Enter PIN if needed ───
if [ -n "$PIN" ]; then
    echo "Entering PIN..."
    gammu --config /etc/gammurc entersecuritycode PIN "$PIN" 2>/dev/null || true
fi

# ─── Create directories ───
mkdir -p /var/spool/gammu/received
mkdir -p /var/log/gammu

# ─── Helper: send AT command, capture response ───
# Uses background echo + timeout cat to avoid race conditions.
send_at() {
    local cmd="$1"
    local device="$2"
    (echo -e "${cmd}\r"; sleep 1) > "$device" 2>/dev/null &
    local bg_pid=$!
    timeout 2 cat "$device" 2>/dev/null || true
    wait "$bg_pid" 2>/dev/null || true
}

# ─── Detect modem manufacturer and apply quiet settings ───
# Unsolicited codes (^RSSI, ^HCSQ, +QIND...) desync gammu's AT parser,
# causing GetNextSMS to hang. We disable them per-vendor and via universal
# 3GPP commands.
echo "Detecting modem and disabling unsolicited notifications..."

MFR_RESPONSE=$(send_at "AT+CGMI" "${DEVICE}" 2>/dev/null || echo "")
MFR=$(echo "$MFR_RESPONSE" | tr -d '\r' | grep -iE "huawei|quectel|sim[c ]?com|telit|zte|u-?blox|cinterion|gemalto" | head -1 | tr '[:upper:]' '[:lower:]')

echo "Detected manufacturer: ${MFR:-unknown}"

case "$MFR" in
    *huawei*)
        echo "Applying Huawei quiet settings..."
        send_at "AT^CURC=0" "${DEVICE}" >/dev/null
        ;;
    *quectel*)
        echo "Applying Quectel quiet settings..."
        send_at 'AT+QURCCFG="urcport","usbat"' "${DEVICE}" >/dev/null
        send_at 'AT+QINDCFG="all",0,1' "${DEVICE}" >/dev/null
        ;;
    *sim*com*)
        echo "Applying SIMCom quiet settings..."
        send_at "AT+CURC=0" "${DEVICE}" >/dev/null
        ;;
    *telit*)
        echo "Applying Telit quiet settings..."
        send_at "AT#NITZ=0" "${DEVICE}" >/dev/null
        ;;
    *zte*)
        echo "Applying ZTE quiet settings..."
        send_at "AT+ZOPRT=5" "${DEVICE}" >/dev/null
        ;;
    *)
        echo "Unknown manufacturer - skipping vendor-specific commands"
        ;;
esac

# Universal 3GPP commands (work on ALL standards-compliant modems)
echo "Applying universal 3GPP settings..."
send_at "AT+CNMI=0,0,0,0,0" "${DEVICE}" >/dev/null
send_at "AT+CMEE=1" "${DEVICE}" >/dev/null

echo "Modem initialization complete"
sleep 1

# ─── Export settings for the Python app ───
export GAMMU_CONFIG="/etc/gammurc"
export WEBHOOK_URL
export API_USER
export API_PASS
export POLL_INTERVAL
export SIGNAL_REFRESH

echo "Starting API server..."
exec uvicorn main:app --host 0.0.0.0 --port 5000 --log-level info
