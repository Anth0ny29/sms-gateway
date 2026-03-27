# Guide des commandes AT pour modems USB GSM/3G/4G

Guide pratique pour utiliser un modem USB avec les commandes AT sous Linux.
Testé avec Huawei E3272, applicable à la plupart des modems compatibles AT (Huawei, Waveshare SIM7600, Quectel, ZTE, etc.).

---

## Première utilisation d'un modem USB

### 1. Brancher et vérifier la détection

```bash
# Le modem est-il détecté par le système ?
lsusb | grep -i -E "huawei|simcom|quectel|zte|option"

# Quels ports série sont apparus ?
ls /dev/ttyUSB*
```

Vous verrez un ou plusieurs ports (ex: `ttyUSB0`, `ttyUSB1`, `ttyUSB2`).
Certains modems exposent 2 à 4 ports : un seul est le port AT, les autres servent au diagnostic ou aux données.

> **Si rien n'apparaît :** le modem est peut-être en mode CD-ROM (comportement par défaut de beaucoup de clés Huawei).
> Installer `usb-modeswitch` pour le basculer en mode modem :
> ```bash
> sudo apt install usb-modeswitch usb-modeswitch-data
> # Débrancher / rebrancher le modem
> ls /dev/ttyUSB*
> ```

### 2. Trouver le bon port et la bonne vitesse

Chaque port doit être testé. Essayez dans cet ordre : `ttyUSB0`, `ttyUSB1`, `ttyUSB2`...

```bash
stty -F /dev/ttyUSB0 9600 raw -echo
echo -e "AT\r" > /dev/ttyUSB0 && timeout 2 cat /dev/ttyUSB0
```

Si vous voyez `OK` → c'est le bon port et la bonne vitesse.

Sinon, essayez une autre vitesse :

```bash
# 19200 baud
stty -F /dev/ttyUSB0 19200 raw -echo
echo -e "AT\r" > /dev/ttyUSB0 && timeout 2 cat /dev/ttyUSB0

# 115200 baud
stty -F /dev/ttyUSB0 115200 raw -echo
echo -e "AT\r" > /dev/ttyUSB0 && timeout 2 cat /dev/ttyUSB0
```

> **Astuce :** les anciens Huawei (E220, E1750, E3272) fonctionnent souvent à 9600 ou 19200.
> Les modems récents (SIM7600, Quectel) fonctionnent à 115200.

### 3. Identifier le modem

Une fois le bon port trouvé :

```bash
# Fabricant
echo -e "ATI\r" > /dev/ttyUSB1 && sleep 1 && timeout 2 cat /dev/ttyUSB1

# Identité complète (avec gammu, si installé)
gammu --identify
```

---

## Commandes AT — Cheatsheet

Pour toutes les commandes ci-dessous, remplacez `/dev/ttyUSB1` par votre port et adaptez la vitesse.

Envoyer une commande :
```bash
echo -e "COMMANDE\r" > /dev/ttyUSB1 && sleep 1 && timeout 3 cat /dev/ttyUSB1
```

---

### Vérification de base

| Commande | Description | Réponse attendue |
|---|---|---|
| `AT` | Test de communication | `OK` |
| `ATI` | Identité du modem (fabricant, modèle, firmware) | Texte libre |
| `AT+CGSN` | Numéro IMEI | `86xxxxxxxxx` |
| `AT+CIMI` | Numéro IMSI de la SIM | `208xxxxxxxxx` |
| `AT+CCID` | Numéro ICCID de la SIM | `89xxxxxxxxxxxx` |

---

### État de la SIM

| Commande | Description | Réponse attendue |
|---|---|---|
| `AT+CPIN?` | État du code PIN | `READY` = OK, `SIM PIN` = en attente de PIN |
| `AT+CPIN=1234` | Entrer le code PIN | `OK` |
| `AT+CLCK="SC",0,"1234"` | Désactiver le PIN (1234 = votre PIN) | `OK` |
| `AT+CLCK="SC",1,"1234"` | Réactiver le PIN | `OK` |

---

### Réseau et enregistrement

| Commande | Description | Réponse attendue |
|---|---|---|
| `AT+CREG?` | État d'enregistrement réseau | Voir tableau ci-dessous |
| `AT+COPS?` | Opérateur actuel | `+COPS: 0,0,"Free"` |
| `AT+COPS=?` | Scanner les réseaux disponibles (30-60s) | Liste des opérateurs |
| `AT+COPS=0` | Sélection auto de l'opérateur | `OK` |
| `AT+COPS=1,2,"20815"` | Forcer un opérateur (ici Free) | `OK` |

**Codes opérateurs France :**

| Code | Opérateur |
|---|---|
| 20801 | Orange |
| 20810 | SFR |
| 20815 | Free Mobile |
| 20816 | Free (réseau secondaire) |
| 20820 | Bouygues Telecom |

**Comprendre `AT+CREG?` :**

La réponse est `+CREG: X,Y` — c'est **Y** qui compte :

| Y | Signification |
|---|---|
| 0 | Pas enregistré, ne cherche pas |
| 1 | ✅ Enregistré, réseau national |
| 2 | ⏳ Pas enregistré, en recherche... |
| 3 | ❌ Enregistrement refusé |
| 5 | ✅ Enregistré, en roaming |

**Comprendre `AT+COPS=?` :**

Chaque réseau est affiché comme : `(statut, "nom", "abrégé", "code", technologie)`

| Statut | Signification |
|---|---|
| 0 | Inconnu |
| 1 | Disponible |
| 2 | ✅ Connecté actuellement |
| 3 | ❌ Interdit (la SIM n'a pas le droit) |

| Technologie | Signification |
|---|---|
| 0 | 2G (GSM) |
| 2 | 3G (UMTS/WCDMA) |
| 7 | 4G (LTE) |

---

### Signal

| Commande | Description | Réponse attendue |
|---|---|---|
| `AT+CSQ` | Force du signal | `+CSQ: X,Y` |

**Comprendre `AT+CSQ` :**

Le premier chiffre (X) indique la force du signal :

| X | dBm | Qualité |
|---|---|---|
| 0-1 | < -109 | Pas de signal |
| 2-9 | -109 à -93 | Mauvais |
| 10-14 | -93 à -81 | Correct |
| 15-19 | -81 à -73 | Bon |
| 20-30 | -73 à -53 | Très bon |
| 31 | > -51 | Excellent |
| 99 | — | Pas de mesure |

---

### Mode réseau (2G / 3G / 4G)

C'est ici que ça diffère selon les fabricants.

#### Huawei ancienne génération (E220, E1750, E3272 avec firmware 21.xxx)

Vérifier le mode actuel :

```bash
echo -e "AT^SYSCFG?\r" > /dev/ttyUSB1 && sleep 1 && timeout 3 cat /dev/ttyUSB1
```

| Commande | Mode |
|---|---|
| `AT^SYSCFG=2,0,3FFFFFFF,1,2` | Auto (2G + 3G) |
| `AT^SYSCFG=13,1,3FFFFFFF,1,2` | 2G uniquement |
| `AT^SYSCFG=14,2,3FFFFFFF,1,2` | 3G uniquement |
| `AT^SYSCFG=2,1,3FFFFFFF,1,2` | 2G préféré |
| `AT^SYSCFG=2,2,3FFFFFFF,1,2` | 3G préféré |

> ⚠️ `AT^SYSCFG` ne gère PAS la 4G. Si votre modem est 4G, utilisez `AT^SYSCFGEX` ci-dessous.

#### Huawei avec support 4G (E3272, E3372, E392, E398)

Vérifier le mode actuel :

```bash
echo -e "AT^SYSCFGEX?\r" > /dev/ttyUSB1 && sleep 1 && timeout 3 cat /dev/ttyUSB1
```

La réponse contient un code mode comme `"00"`, `"02"`, etc.

| Commande | Mode |
|---|---|
| `AT^SYSCFGEX="00",3FFFFFFF,1,2,7FFFFFFFFFFFFFFF,,` | ✅ Auto (4G > 3G > 2G) |
| `AT^SYSCFGEX="01",3FFFFFFF,1,2,7FFFFFFFFFFFFFFF,,` | 2G uniquement |
| `AT^SYSCFGEX="02",3FFFFFFF,1,2,7FFFFFFFFFFFFFFF,,` | 3G uniquement |
| `AT^SYSCFGEX="03",3FFFFFFF,1,2,7FFFFFFFFFFFFFFF,,` | 4G uniquement |
| `AT^SYSCFGEX="0302",3FFFFFFF,1,2,7FFFFFFFFFFFFFFF,,` | 4G préféré, sinon 3G |
| `AT^SYSCFGEX="030201",3FFFFFFF,1,2,7FFFFFFFFFFFFFFF,,` | 4G > 3G > 2G (explicite) |

**Codes mode :**

| Code | Réseau |
|---|---|
| `00` | Automatique |
| `01` | 2G (GSM) |
| `02` | 3G (WCDMA) |
| `03` | 4G (LTE) |

> Les codes peuvent être combinés pour définir un ordre de préférence : `"0302"` = essayer la 4G d'abord, puis la 3G.

#### Waveshare SIM7600 / SIMCom

```bash
# Vérifier le mode
echo -e "AT+CNMP?\r" > /dev/ttyUSB2 && sleep 1 && timeout 3 cat /dev/ttyUSB2
```

| Commande | Mode |
|---|---|
| `AT+CNMP=2` | Auto |
| `AT+CNMP=13` | 2G uniquement |
| `AT+CNMP=14` | 3G uniquement |
| `AT+CNMP=38` | 4G uniquement |

---

### SMS

| Commande | Description |
|---|---|
| `AT+CMGF=1` | Passer en mode texte (plus lisible) |
| `AT+CMGF=0` | Passer en mode PDU (par défaut) |
| `AT+CMGL="ALL"` | Lister tous les SMS (en mode texte) |
| `AT+CMGR=1` | Lire le SMS n°1 |
| `AT+CMGD=1` | Supprimer le SMS n°1 |
| `AT+CMGD=1,4` | Supprimer tous les SMS |
| `AT+CPMS?` | Voir la capacité mémoire SMS |

**Envoyer un SMS (en mode texte) :**

```bash
echo -e "AT+CMGF=1\r" > /dev/ttyUSB1
sleep 1
echo -e "AT+CMGS=\"+33612345678\"\r" > /dev/ttyUSB1
sleep 1
echo -e "Bonjour !\x1a" > /dev/ttyUSB1
```

Le `\x1a` (Ctrl+Z) termine et envoie le message.

---

### Redémarrage et reset

| Commande | Description |
|---|---|
| `AT+CFUN=0` | Éteindre le module radio (mode avion) |
| `AT+CFUN=1` | Rallumer le module radio |
| `AT+CFUN=1,1` | Redémarrer complètement le modem |

**Procédure de reset complet :**

```bash
# 1. Éteindre la radio
echo -e "AT+CFUN=0\r" > /dev/ttyUSB1
sleep 5

# 2. Rallumer
echo -e "AT+CFUN=1\r" > /dev/ttyUSB1
sleep 20

# 3. Vérifier
echo -e "AT+CREG?\r" > /dev/ttyUSB1 && sleep 2 && timeout 3 cat /dev/ttyUSB1
```

Si ça ne suffit pas, le hard reboot :

```bash
echo -e "AT+CFUN=1,1\r" > /dev/ttyUSB1
sleep 30
# Le port ttyUSB peut changer après reboot !
ls /dev/ttyUSB*
```

---

### Commandes utiles diverses

| Commande | Description |
|---|---|
| `AT+CGDCONT?` | Voir les APN configurés |
| `AT+CGDCONT=1,"IP","free"` | Configurer l'APN (ici Free) |
| `AT+CUSD=1,"*100#"` | Envoyer un code USSD (vérifier solde, etc.) |
| `AT+CLVL=?` | Voir les niveaux de volume supportés |
| `AT&V` | Voir la configuration complète du modem |
| `AT&F` | Restaurer les paramètres usine |
| `ATZ` | Reset aux paramètres sauvegardés |

---

## Problèmes courants et solutions

### Le modem ne s'enregistre pas sur le réseau (`CREG: 0,2`)

**Cause probable :** mauvais mode réseau. Si le modem est forcé en 3G mais que votre opérateur n'a que la 4G dans votre zone.

**Solution :** passer en mode auto.

```bash
# Huawei avec 4G
echo -e 'AT^SYSCFGEX="00",3FFFFFFF,1,2,7FFFFFFFFFFFFFFF,,\r' > /dev/ttyUSB1
sleep 5
echo -e "AT+COPS=0\r" > /dev/ttyUSB1
sleep 30
echo -e "AT+CREG?\r" > /dev/ttyUSB1 && sleep 2 && timeout 3 cat /dev/ttyUSB1
```

### Le modem perd le réseau après un certain temps

**Solution :** redémarrer le module radio.

```bash
echo -e "AT+CFUN=0\r" > /dev/ttyUSB1
sleep 5
echo -e "AT+CFUN=1\r" > /dev/ttyUSB1
sleep 20
```

Si ça ne marche pas, essayer un reset USB logiciel :

```bash
# Trouver le chemin sysfs du modem
grep -r "12d1" /sys/bus/usb/devices/*/idVendor 2>/dev/null
# Résultat exemple : /sys/bus/usb/devices/1-3/idVendor

# Débrancher / rebrancher logiciel
echo 0 > /sys/bus/usb/devices/1-3/authorized
sleep 2
echo 1 > /sys/bus/usb/devices/1-3/authorized
sleep 15
```

### Signal faible (`CSQ: 5,99`)

Essayez de déplacer le modem (rallonge USB) ou de forcer un mode réseau différent. La 2G a souvent une meilleure portée que la 4G :

```bash
# Forcer 2G temporairement
echo -e 'AT^SYSCFGEX="01",3FFFFFFF,1,2,7FFFFFFFFFFFFFFF,,\r' > /dev/ttyUSB1
sleep 10
echo -e "AT+CSQ\r" > /dev/ttyUSB1 && sleep 1 && timeout 3 cat /dev/ttyUSB1
```

### `COMMAND NOT SUPPORT`

La commande n'est pas supportée par votre modem. Les commandes `AT^SYSCFG` / `AT^SYSCFGEX` sont spécifiques Huawei. Les modems SIMCom utilisent `AT+CNMP`. Référez-vous à la section correspondant à votre fabricant.

### Buffer série corrompu (réponses mélangées)

```bash
# Vider le buffer
stty -F /dev/ttyUSB1 9600 raw -echo
echo -e "AT\r" > /dev/ttyUSB1
sleep 2
echo -e "AT\r" > /dev/ttyUSB1 && sleep 1 && timeout 2 cat /dev/ttyUSB1
# Répéter jusqu'à obtenir un OK propre
```

---

## Récapitulatif rapide

```
AT                     → le modem répond ?
AT+CPIN?               → la SIM est prête ?
AT+CREG?               → enregistré sur le réseau ?
AT+COPS?               → quel opérateur ?
AT+CSQ                 → quel signal ?
AT^SYSCFGEX?           → quel mode réseau ? (Huawei 4G)
AT^SYSCFGEX="00",...   → passer en auto
AT+CFUN=1,1            → redémarrer le modem
```
