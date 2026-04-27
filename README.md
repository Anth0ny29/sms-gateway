# 📱 Gammu SMS Gateway

Send and receive SMS from Docker using a USB modem. Works with Home Assistant.

Drop-in replacement for [pajikos/sms-gammu-gateway](https://github.com/pajikos/sms-gammu-gateway) with everything it was missing.

---

## What it does

- **Send SMS** from any system via a REST API
- **Receive SMS** with instant webhook notifications
- **Long messages, emojis** : handled automatically
- **Monitor the modem** : signal, network, operator, counters
- **Home Assistant** : send, receive, SMS commands, sensors
- **Docker** : single container, no MQTT, no external dependencies

---

## Getting Started

### Step 1 — Find your modem

Plug in your USB dongle, stop any running container (`docker stop sms-gateway`), then run:
```bash
found=0; for d in /dev/ttyUSB* /dev/ttyACM*; do [ -e "$d" ] || continue; for b in 9600 19200 115200; do stty -F "$d" "$b" raw -echo min 0 time 5 2>/dev/null || continue; r="$( (printf "AT\r" >"$d"; timeout 1 cat "$d") 2>/dev/null | tr -d "\r" )"; if echo "$r" | grep -q "OK"; then found=1; printf "\n# Valid modem configuration\n%s\n%s\n" "device: $d" "CONNECTION: at$b"; fi; done; done; [ "$found" -eq 1 ] || echo "⚠️ Warning: No valid modem port/baudrate combination found."
```

Example output:
```
# Valid modem configuration
device: /dev/ttyUSB2
CONNECTION: at9600
# Valid modem configuration
device: /dev/ttyUSB3
CONNECTION: at9600
```

If multiple ports show up, use the first one. Note the `device` and `CONNECTION` values — you'll need them in the next step.

### Step 2 — Configure

Edit `docker-compose.yml` with the values found above:
```yaml
services:
  sms-gateway:
    image: kyukiblade/sms-gateway:latest
    container_name: sms-gateway
    restart: unless-stopped
    ports:
      - "5000:5000"
    devices:
      - /dev/ttyUSB2:/dev/mobile       # ← your port from step 1
    environment:
      - DEVICE=/dev/mobile
      - CONNECTION=at9600              # ← your speed from step 1
      - PIN=                           # SIM PIN (empty = no PIN)
      - API_USER=admin
      - API_PASS=changeme              # ← change this
      - WEBHOOK_URL=http://192.168.1.x:8123/api/webhook/sms_received
      - POLL_INTERVAL=3
      - SIGNAL_REFRESH=60
    volumes:
      - sms-data:/var/spool/gammu/received

volumes:
  sms-data:
```

### Step 3 — Run

**From Docker Hub (easiest):**
```bash
docker compose up -d
```

**Or build from source:**
```bash
git clone https://github.com/Anth0ny29/sms-gateway.git
cd sms-gateway
docker compose up -d --build
```

### Step 4 — Verify
```bash
curl http://localhost:5000/api/health
```

Expected: `{"status":"ok","modem_active":true,"receiver_running":true,...}`

---

## Quick usage

### Send an SMS
```bash
curl -u admin:changeme \
  -H "Content-Type: application/json" \
  -X POST http://localhost:5000/api/sms \
  -d '{"number": "+1234567890", "text": "Hello!"}'
```

### View received SMS
```bash
curl -u admin:changeme http://localhost:5000/api/sms
```

### Check modem status
```bash
curl -u admin:changeme http://localhost:5000/api/modem/status
```

---

## API Reference

All endpoints require HTTP Basic Auth except `/api/health`.

Interactive Swagger docs: `http://<ip>:5000/docs`

---

### `POST /api/sms` — Send an SMS

Long messages and emojis are handled automatically.
```bash
curl -u admin:changeme \
  -H "Content-Type: application/json" \
  -X POST http://localhost:5000/api/sms \
  -d '{"number": "+1234567890", "text": "My message", "smsc": ""}'
```

| Field | Required | Description |
|---|---|---|
| `number` | ✅ | Recipient number (international format) |
| `text` | ✅ | Message content (no size limit) |
| `smsc` | ❌ | SMS center override (rarely needed) |

Response:
```json
{"status": "sent", "message": "Sent 2 part(s)", "number": "+1234567890"}
```

---

### `GET /api/sms` — List received SMS
```bash
curl -u admin:changeme "http://localhost:5000/api/sms?limit=20"
```

| Parameter | Default | Description |
|---|---|---|
| `limit` | 50 | Max number of messages |

Response:
```json
[
  {
    "id": "20260315_170530_123456_+1234567890",
    "timestamp": "2026-03-15T17:05:30Z",
    "number": "+1234567890",
    "text": "Message content",
    "sms_class": null,
    "parts": 1
  }
]
```

---

### `GET /api/sms/{id}` — Get a specific SMS
```bash
curl -u admin:changeme http://localhost:5000/api/sms/20260315_170530_123456_+1234567890
```

Same format as a list item. Returns 404 if the ID doesn't exist.

---

### `DELETE /api/sms/{id}` — Delete an SMS
```bash
curl -u admin:changeme -X DELETE http://localhost:5000/api/sms/20260315_170530_123456_+1234567890
```

Response: `{"status": "deleted", "id": "..."}`

---

### `GET /api/modem/status` — Full modem status
```bash
curl -u admin:changeme http://localhost:5000/api/modem/status
```

Response:
```json
{
  "modem_active": true,
  "device": "/dev/mobile",
  "manufacturer": "Huawei",
  "model": "E3272 (E3272)",
  "firmware": "21.xxx",
  "imei": "86xxxxxxxxx",
  "imsi": "208xxxxxxxxx",
  "network_state": "RoamingNetwork",
  "network_name": "Orange F",
  "network_code": "208 01",
  "gprs": "Attached",
  "signal_percent": 24,
  "signal_dbm": -93,
  "battery_percent": 0,
  "sms_sent": 3,
  "sms_received": 5,
  "sms_failed": 0,
  "worker_respawns": 0
}
```

| Field | Description |
|---|---|
| `modem_active` | Modem is connected and responding |
| `manufacturer` | Modem manufacturer |
| `model` | Modem model |
| `imei` | Modem unique identifier |
| `imsi` | SIM card identifier |
| `network_state` | Network state (`HomeNetwork`, `RoamingNetwork`...) |
| `network_name` | Operator name |
| `signal_percent` | Signal strength (0-100%) |
| `signal_dbm` | Signal strength in dBm |
| `sms_sent` | SMS sent since startup |
| `sms_received` | SMS received since startup |
| `sms_failed` | Failed sends |
| `worker_respawns` | Number of times the modem worker was automatically restarted |

---

### `GET /api/modem/signal` — Modem signal

Lightweight version with signal and counters only.
```bash
curl -u admin:changeme http://localhost:5000/api/modem/signal
```

Response:
```json
{
  "signal_percent": 24,
  "signal_dbm": -93,
  "battery_percent": 0,
  "network_name": "Orange F",
  "sms_sent": 3,
  "sms_received": 5,
  "sms_failed": 0
}
```

---

### `POST /api/ussd` — Send a USSD code

Check balance, activate options, etc.
```bash
curl -u admin:changeme \
  -H "Content-Type: application/json" \
  -X POST http://localhost:5000/api/ussd \
  -d '{"code": "*100#"}'
```

Response: `{"status": "ok", "response": "Your balance is 15.30 EUR"}`

---

### `GET /api/health` — Health check

**No authentication required.** Used by Docker to check the container is working.
```bash
curl http://localhost:5000/api/health
```

Response:
```json
{
  "status": "ok",
  "modem_active": true,
  "receiver_running": true,
  "worker_respawns": 0,
  "timestamp": "2026-03-15T17:05:30+00:00"
}
```

---

### `GET /api/daemon/logs` — Logs
```bash
curl -u admin:changeme "http://localhost:5000/api/daemon/logs?lines=50"
```

---

### pajikos compatibility

If you're migrating from pajikos, the old endpoints still work:

| Old | New | Description |
|---|---|---|
| `POST /sms` | `POST /api/sms` | Send SMS |
| `GET /getsms` | `GET /api/sms` | List SMS |

---

## Webhook — Get notified when an SMS arrives

When an SMS arrives on the modem, the gateway automatically calls the URL set in `WEBHOOK_URL`.

### What gets sent
```json
{
  "event": "sms_received",
  "timestamp": "2026-03-15T17:05:30Z",
  "number": "+1234567890",
  "text": "Message content",
  "class": "",
  "parts": 1
}
```

### Latency

Between **3 and 8 seconds** after receiving the SMS. Depends on modem speed and `POLL_INTERVAL`.

### Test the webhook without a modem
```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"event":"sms_received","number":"+1234567890","text":"Test webhook"}' \
  http://192.168.1.x:8123/api/webhook/sms_received
```

---

## Home Assistant

### Send SMS from HA

In `configuration.yaml`:
```yaml
rest_command:
  send_sms:
    url: "http://192.168.1.x:5000/api/sms"
    method: POST
    content_type: "application/json"
    username: "admin"
    password: "changeme"
    payload: '{"number": "{{ number }}", "text": "{{ message }}"}'
```

In an automation:
```yaml
automation:
  - alias: "Alarm → SMS"
    trigger:
      - platform: state
        entity_id: alarm_control_panel.home
        to: "triggered"
    action:
      - service: rest_command.send_sms
        data:
          number: "+1234567890"
          message: "ALARM triggered at {{ now().strftime('%H:%M') }}!"
```

### Receive SMS in HA

The HA webhook **does not require an API key**. The `webhook_id` acts as the secret.

In `docker-compose.yml`:
```yaml
- WEBHOOK_URL=http://192.168.1.x:8123/api/webhook/sms_received
```

In HA:
```yaml
automation:
  - alias: "SMS received"
    trigger:
      - platform: webhook
        webhook_id: sms_received
        allowed_methods: [POST]
        local_only: true
    action:
      - service: notify.mobile_app_phone
        data:
          title: "SMS from {{ trigger.json.number }}"
          message: "{{ trigger.json.text }}"
```

### Control your home by SMS
```yaml
automation:
  - alias: "SMS commands"
    trigger:
      - platform: webhook
        webhook_id: sms_received
        allowed_methods: [POST]
        local_only: true
    condition:
      - condition: template
        value_template: "{{ trigger.json.number == '+1234567890' }}"
    action:
      - choose:
          - conditions:
              - condition: template
                value_template: "{{ 'ALARM ON' in trigger.json.text | upper }}"
            sequence:
              - service: alarm_control_panel.alarm_arm_away
                target:
                  entity_id: alarm_control_panel.home
          - conditions:
              - condition: template
                value_template: "{{ 'STATUS' in trigger.json.text | upper }}"
            sequence:
              - service: rest_command.send_sms
                data:
                  number: "{{ trigger.json.number }}"
                  message: >
                    Alarm: {{ states('alarm_control_panel.home') }}
                    Temp: {{ states('sensor.temperature') }}°C
```

### Monitor the modem in HA
```yaml
sensor:
  - platform: rest
    name: "GSM Signal"
    resource: "http://192.168.1.x:5000/api/modem/signal"
    authentication: basic
    username: admin
    password: changeme
    value_template: "{{ value_json.signal_percent }}"
    unit_of_measurement: "%"
    icon: mdi:signal-cellular-2
    scan_interval: 120
    json_attributes:
      - signal_dbm
      - battery_percent
      - network_name
      - sms_sent
      - sms_received
      - sms_failed

binary_sensor:
  - platform: rest
    name: "SMS Gateway"
    resource: "http://192.168.1.x:5000/api/health"
    value_template: "{{ value_json.modem_active }}"
    device_class: connectivity
    scan_interval: 60
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DEVICE` | `/dev/mobile` | Modem path inside the container |
| `CONNECTION` | `at` | Speed: `at9600`, `at19200`, `at115200` |
| `PIN` | *(empty)* | SIM PIN code |
| `API_USER` | `admin` | API username |
| `API_PASS` | `admin` | API password |
| `WEBHOOK_URL` | *(empty)* | URL called on each received SMS |
| `POLL_INTERVAL` | `3` | SMS check interval (seconds) |
| `SIGNAL_REFRESH` | `60` | Signal refresh interval (seconds) |

---

## Project files
```
sms-gateway/
├── Dockerfile               # Docker image
├── docker-compose.yml       # Configuration
├── docker-compose.build.yml # Build from source
├── requirements.txt         # Python dependencies
├── LICENSE
├── README.md                # English docs
├── README.fr.md             # French docs
├── AT-COMMANDS.md           # AT commands cheatsheet
└── app/
    ├── entrypoint.sh        # Startup script
    └── main.py              # Complete application
```

---

## Troubleshooting

### "Modem not available"

Re-run the detection command from Step 1. Make sure no other container is using the modem:
```bash
docker ps -a | grep sms
```

### Container keeps restarting
```bash
docker compose logs -f sms-gateway
```

### Webhook not working
```bash
docker exec sms-gateway env | grep WEBHOOK
```

Test manually:
```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"event":"sms_received","number":"+1234567890","text":"Test"}' \
  http://192.168.1.x:8123/api/webhook/sms_received
```

### Clean up stored SMS
```bash
docker compose down
docker volume rm <prefix>_sms-data
docker compose up -d
```

---

## License

MIT
