# 📱 Gammu SMS Gateway

Envoyez et recevez des SMS depuis Docker avec un simple modem USB. Fonctionne avec Home Assistant.

Remplacement de [pajikos/sms-gammu-gateway](https://github.com/pajikos/sms-gammu-gateway) avec tout ce qui lui manquait.

---

## Ce que ça fait

- **Envoyer des SMS** depuis n'importe quel système via une API REST
- **Recevoir des SMS** et être notifié instantanément (webhook)
- **SMS longs, émojis** : tout passe, le découpage est automatique
- **Surveiller le modem** : signal, réseau, opérateur, compteurs
- **Home Assistant** : envoi, réception, commandes par SMS, capteurs
- **Docker** : un seul conteneur, pas de MQTT, pas de dépendance externe

---

## Installation

### Option A — Depuis Docker Hub (le plus simple)

```bash
docker pull VOTRE_USER/sms-gateway:latest
```

Téléchargez le [`docker-compose.yml`](docker-compose.yml), éditez les 3 valeurs essentielles (port modem, vitesse, mot de passe), puis :

```bash
docker compose up -d
```

### Option B — Builder depuis les sources

```bash
git clone https://github.com/VOTRE_USER/sms-gateway.git
cd sms-gateway
docker compose up -d --build
```

### Identifier le modem

#### Étape 1 — Trouver votre modem

Branchez votre clé USB, puis sur la machine hôte :

```bash
ls /dev/ttyUSB*
```

Vous verrez un ou plusieurs ports (`ttyUSB0`, `ttyUSB1`...). Testez lequel répond :

```bash
stty -F /dev/ttyUSB0 9600 raw -echo
echo -e "AT\r" > /dev/ttyUSB0 && timeout 2 cat /dev/ttyUSB0
```

Si vous voyez `OK`, c'est le bon port. Sinon, essayez le suivant.

### Étape 2 — Trouver la bonne vitesse

Testez avec différentes vitesses (remplacez `9600` par `19200` ou `115200`). Celle qui retourne `OK` est la bonne.

| Valeur pour `CONNECTION` | Vitesse |
|---|---|
| `at9600` | 9600 baud |
| `at19200` ou `at` | 19200 baud |
| `at115200` | 115200 baud |

### Étape 3 — Configurer

Éditez `docker-compose.yml` avec vos valeurs :

```yaml
services:
  sms-gateway:
    build: .
    container_name: sms-gateway
    restart: unless-stopped
    ports:
      - "5000:5000"
    devices:
      - /dev/ttyUSB1:/dev/mobile       # ← votre port
    environment:
      - DEVICE=/dev/mobile
      - CONNECTION=at19200             # ← votre vitesse
      - PIN=                           # PIN SIM (vide = pas de PIN)
      - API_USER=admin                 # identifiant API
      - API_PASS=changeme              # mot de passe API
      - WEBHOOK_URL=http://192.168.1.x:8123/api/webhook/sms_received
      - POLL_INTERVAL=2
      - SIGNAL_REFRESH=60
    volumes:
      - sms-data:/var/spool/gammu/received

volumes:
  sms-data:
```

### Étape 4 — Lancer

```bash
docker compose up -d --build
```

### Étape 5 — Vérifier

```bash
curl http://localhost:5000/api/health
```

Vous devez voir `"modem_active": true`.

---

## Configuration

| Variable | Défaut | Description |
|---|---|---|
| `DEVICE` | `/dev/mobile` | Chemin du modem dans le conteneur |
| `CONNECTION` | `at` | Vitesse : `at`, `at9600`, `at19200`, `at115200` |
| `PIN` | *(vide)* | Code PIN de la SIM |
| `API_USER` | `admin` | Identifiant pour l'API |
| `API_PASS` | `admin` | Mot de passe pour l'API |
| `WEBHOOK_URL` | *(vide)* | URL appelée à chaque SMS reçu |
| `POLL_INTERVAL` | `2` | Fréquence de vérification des SMS (en secondes) |
| `SIGNAL_REFRESH` | `60` | Fréquence de mise à jour du signal (en secondes) |

> **PIN** : ne mettez pas de guillemets autour de la valeur. `PIN=1234` ✅ — `PIN="1234"` ❌

---

## Utilisation rapide

### Envoyer un SMS

```bash
curl -u admin:changeme \
  -H "Content-Type: application/json" \
  -X POST http://localhost:5000/api/sms \
  -d '{"number": "+33612345678", "text": "Bonjour !"}'
```

### Voir les SMS reçus

```bash
curl -u admin:changeme http://localhost:5000/api/sms
```

### Voir l'état du modem

```bash
curl -u admin:changeme http://localhost:5000/api/modem/status
```

---

## API — Référence complète

Tous les endpoints nécessitent une authentification HTTP Basic sauf `/api/health`.

Documentation interactive Swagger : `http://<ip>:5000/docs`

---

### `POST /api/sms` — Envoyer un SMS

Les SMS longs et les émojis sont gérés automatiquement.

```bash
curl -u admin:changeme \
  -H "Content-Type: application/json" \
  -X POST http://localhost:5000/api/sms \
  -d '{"number": "+33612345678", "text": "Mon message", "smsc": "+33695000695"}'
```

| Champ | Requis | Description |
|---|---|---|
| `number` | ✅ | Numéro du destinataire (format international) |
| `text` | ✅ | Texte du message (pas de limite de taille) |
| `smsc` | ❌ | Centre SMS override (rarement nécessaire) |

Réponse :

```json
{"status": "sent", "message": "Sent 2 part(s)", "number": "+33612345678"}
```

---

### `GET /api/sms` — Lister les SMS reçus

```bash
curl -u admin:changeme "http://localhost:5000/api/sms?limit=20"
```

| Paramètre | Défaut | Description |
|---|---|---|
| `limit` | 50 | Nombre max de SMS retournés |

Réponse :

```json
[
  {
    "id": "20260315_170530_123456_+33612345678",
    "timestamp": "2026-03-15T17:05:30Z",
    "number": "+33612345678",
    "text": "Contenu du SMS",
    "sms_class": null,
    "parts": 1
  }
]
```

---

### `GET /api/sms/{id}` — Voir un SMS

```bash
curl -u admin:changeme http://localhost:5000/api/sms/20260315_170530_123456_+33612345678
```

Même format qu'un élément de la liste. Retourne 404 si l'ID n'existe pas.

---

### `DELETE /api/sms/{id}` — Supprimer un SMS

```bash
curl -u admin:changeme -X DELETE http://localhost:5000/api/sms/20260315_170530_123456_+33612345678
```

Réponse : `{"status": "deleted", "id": "..."}`

---

### `GET /api/modem/status` — Statut complet du modem

```bash
curl -u admin:changeme http://localhost:5000/api/modem/status
```

Réponse :

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
  "sms_failed": 0
}
```

| Champ | Description |
|---|---|
| `modem_active` | Le modem est connecté et répond |
| `manufacturer` | Fabricant du modem |
| `model` | Modèle |
| `imei` | Identifiant unique du modem |
| `imsi` | Identifiant de la carte SIM |
| `network_state` | État réseau (`HomeNetwork`, `RoamingNetwork`...) |
| `network_name` | Nom de l'opérateur |
| `signal_percent` | Force du signal (0-100%) |
| `signal_dbm` | Force du signal en dBm |
| `sms_sent` | SMS envoyés depuis le démarrage |
| `sms_received` | SMS reçus depuis le démarrage |
| `sms_failed` | Envois échoués |

---

### `GET /api/modem/signal` — Signal du modem

Version allégée avec le signal et les compteurs.

```bash
curl -u admin:changeme http://localhost:5000/api/modem/signal
```

Réponse :

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

### `POST /api/ussd` — Envoyer un code USSD

Pour vérifier un solde ou activer une option opérateur.

```bash
curl -u admin:changeme \
  -H "Content-Type: application/json" \
  -X POST http://localhost:5000/api/ussd \
  -d '{"code": "*100#"}'
```

Réponse : `{"status": "ok", "response": "Votre solde est de 15.30 EUR"}`

---

### `GET /api/health` — Health check

**Pas d'authentification.** Utilisé par Docker pour vérifier que le conteneur fonctionne.

```bash
curl http://localhost:5000/api/health
```

Réponse :

```json
{
  "status": "ok",
  "modem_active": true,
  "receiver_running": true,
  "timestamp": "2026-03-15T17:05:30+00:00"
}
```

---

### `GET /api/daemon/logs` — Logs

```bash
curl -u admin:changeme "http://localhost:5000/api/daemon/logs?lines=50"
```

---

### Compatibilité pajikos

Si vous migrez depuis pajikos, les anciens endpoints fonctionnent toujours :

| Ancien | Nouveau | Description |
|---|---|---|
| `POST /sms` | `POST /api/sms` | Envoyer un SMS |
| `GET /getsms` | `GET /api/sms` | Lister les SMS |

---

## Webhook — Être notifié quand un SMS arrive

Quand un SMS arrive sur le modem, la gateway appelle automatiquement l'URL définie dans `WEBHOOK_URL`.

### Ce qui est envoyé

```json
{
  "event": "sms_received",
  "timestamp": "2026-03-15T17:05:30Z",
  "number": "+33612345678",
  "text": "Contenu du SMS",
  "class": "",
  "parts": 1
}
```

### Délai

Entre **3 et 8 secondes** après réception du SMS. Dépend de la vitesse du modem et de `POLL_INTERVAL`.

### Tester le webhook sans modem

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"event":"sms_received","number":"+33612345678","text":"Test webhook"}' \
  http://192.168.1.x:8123/api/webhook/sms_received
```

---

## Home Assistant

### Envoyer un SMS depuis HA

Dans `configuration.yaml` :

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

Dans une automation :

```yaml
automation:
  - alias: "Alarme → SMS"
    trigger:
      - platform: state
        entity_id: alarm_control_panel.home
        to: "triggered"
    action:
      - service: rest_command.send_sms
        data:
          number: "+33612345678"
          message: "ALARME à {{ now().strftime('%H:%M') }} !"
```

### Recevoir un SMS dans HA

Le webhook HA **ne nécessite pas de clé API**. Le `webhook_id` sert de secret.

Dans `docker-compose.yml` :

```yaml
- WEBHOOK_URL=http://192.168.1.x:8123/api/webhook/sms_received
```

Dans HA :

```yaml
automation:
  - alias: "SMS reçu"
    trigger:
      - platform: webhook
        webhook_id: sms_received
        allowed_methods: [POST]
        local_only: true
    action:
      - service: notify.mobile_app_telephone
        data:
          title: "SMS de {{ trigger.json.number }}"
          message: "{{ trigger.json.text }}"
```

### Piloter la maison par SMS

```yaml
automation:
  - alias: "Commandes SMS"
    trigger:
      - platform: webhook
        webhook_id: sms_received
        allowed_methods: [POST]
        local_only: true
    condition:
      - condition: template
        value_template: "{{ trigger.json.number == '+33612345678' }}"
    action:
      - choose:
          - conditions:
              - condition: template
                value_template: "{{ 'ALARME ON' in trigger.json.text | upper }}"
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
                    Alarme : {{ states('alarm_control_panel.home') }}
                    Temp : {{ states('sensor.temperature') }}°C
```

### Surveiller le modem dans HA

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

## Fichiers du projet

```
gammu-sms-gateway/
├── Dockerfile               # Image Docker
├── docker-compose.yml       # Configuration
├── requirements.txt         # Dépendances Python
├── LICENSE
├── README.md                # Doc anglais
├── README.fr.md             # Doc français
└── app/
    ├── entrypoint.sh        # Démarrage
    └── main.py              # Application
```

---

## Dépannage

### « Modem not available »

Vérifiez le bon port et la bonne vitesse depuis l'hôte :

```bash
stty -F /dev/ttyUSB1 9600 raw -echo
echo -e "AT\r" > /dev/ttyUSB1 && timeout 2 cat /dev/ttyUSB1
```

Essayez 9600, 19200, 115200. Adaptez `CONNECTION` dans docker-compose.yml.

### Le conteneur redémarre en boucle

```bash
docker compose logs -f sms-gateway
```

Cause fréquente : un autre conteneur utilise le même modem.

```bash
docker ps -a | grep sms
```

### Le webhook ne fonctionne pas

```bash
docker exec sms-gateway env | grep WEBHOOK
```

Testez manuellement :

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"event":"sms_received","number":"+33612345678","text":"Test"}' \
  http://192.168.1.x:8123/api/webhook/sms_received
```

### Nettoyer les SMS stockés

```bash
docker compose down
docker volume rm <prefix>_sms-data
docker compose up -d
```

---

## Licence

MIT
