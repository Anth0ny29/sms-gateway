"""
Gammu SMS Gateway - REST API (python-gammu direct mode)
=======================================================
Single-process architecture using python-gammu for both
sending and receiving. No gammu-smsd daemon needed.

- Sending: direct via python-gammu (instant, proper multipart)
- Receiving: background polling thread (configurable interval)
- Webhook: called immediately when a new SMS is detected
- Modem info: cached at startup + refreshed periodically
"""

import glob
import json
import logging
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import gammu
import requests as http_requests
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

# ─── Configuration ───────────────────────────────────────────

class Settings(BaseSettings):
    api_user: str = "admin"
    api_pass: str = "admin"
    gammu_config: str = "/etc/gammurc"
    received_path: str = "/var/spool/gammu/received"
    webhook_url: str = ""
    poll_interval: int = 2  # seconds between receive checks
    signal_refresh: int = 60  # seconds between signal refreshes

settings = Settings()

# ─── Logging ─────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("sms-gateway")

# ─── FastAPI app ─────────────────────────────────────────────

app = FastAPI(
    title="Gammu SMS Gateway",
    description="REST API for sending/receiving SMS via python-gammu with instant webhook support",
    version="2.0.0",
)

security = HTTPBasic()

# ─── Auth ────────────────────────────────────────────────────

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    if (
        credentials.username != settings.api_user
        or credentials.password != settings.api_pass
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials

# ─── Models ──────────────────────────────────────────────────

class SendSMSRequest(BaseModel):
    number: str = Field(..., description="Recipient phone number (international format)")
    text: str = Field(..., description="Message text")
    smsc: Optional[str] = Field(None, description="Override SMSC number")

class SendSMSResponse(BaseModel):
    status: str
    message: str
    number: str

class ReceivedSMS(BaseModel):
    id: str
    timestamp: str
    number: str
    text: str
    sms_class: Optional[str] = Field(None, alias="class")
    parts: int = 1

class ModemStatus(BaseModel):
    modem_active: bool
    device: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    firmware: Optional[str] = None
    imei: Optional[str] = None
    imsi: Optional[str] = None
    network_state: Optional[str] = None
    network_name: Optional[str] = None
    network_code: Optional[str] = None
    gprs: Optional[str] = None
    signal_percent: Optional[int] = None
    signal_dbm: Optional[float] = None
    battery_percent: Optional[int] = None
    sms_sent: int = 0
    sms_received: int = 0
    sms_failed: int = 0

class SignalInfo(BaseModel):
    signal_percent: Optional[int] = None
    signal_dbm: Optional[float] = None
    battery_percent: Optional[int] = None
    network_name: Optional[str] = None
    sms_sent: int = 0
    sms_received: int = 0
    sms_failed: int = 0

class HealthResponse(BaseModel):
    status: str
    modem_active: bool
    receiver_running: bool
    timestamp: str

class USSDRequest(BaseModel):
    code: str = Field(..., description="USSD code to send (e.g. *100#)")

# ─── Modem Manager (thread-safe) ────────────────────────────

class ModemManager:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.lock = threading.Lock()
        self._sm: Optional[gammu.StateMachine] = None
        self._connected = False

        # Counters
        self.sms_sent = 0
        self.sms_received = 0
        self.sms_failed = 0

        # Cached modem info
        self.modem_info: dict = {}
        self.signal_info: dict = {}
        self._last_signal_refresh = 0

        # Receiver thread control
        self._receiver_running = False
        self._receiver_thread: Optional[threading.Thread] = None

    def connect(self):
        """Initialize connection to the modem."""
        with self.lock:
            return self._connect_locked()

    def _connect_locked(self) -> bool:
        """Connect (must hold lock)."""
        try:
            if self._sm and self._connected:
                return True
            self._sm = gammu.StateMachine()
            self._sm.ReadConfig(Filename=self.config_path)
            self._sm.Init()
            self._connected = True
            logger.info("Modem connected")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to modem: {e}")
            self._connected = False
            self._sm = None
            return False

    def _reconnect_locked(self) -> bool:
        """Force reconnect (must hold lock)."""
        try:
            if self._sm:
                self._sm.Terminate()
        except Exception:
            pass
        self._connected = False
        self._sm = None
        return self._connect_locked()

    @contextmanager
    def device(self):
        """Context manager for thread-safe modem access with auto-reconnect."""
        with self.lock:
            if not self._connected:
                self._connect_locked()
            if not self._connected:
                raise RuntimeError("Modem not available")
            try:
                yield self._sm
            except (gammu.ERR_TIMEOUT, gammu.ERR_DEVICENOTEXIST) as e:
                logger.warning(f"Modem error, reconnecting: {e}")
                self._reconnect_locked()
                raise
            except Exception:
                raise

    def send_sms(self, number: str, text: str, smsc: Optional[str] = None) -> str:
        """Send an SMS with proper multipart concatenation."""
        with self.device() as sm:
            smsinfo = {
                "Class": -1,
                "Unicode": True,
                "Entries": [{"ID": "ConcatenatedTextLong", "Buffer": text}],
            }
            encoded = gammu.EncodeSMS(smsinfo)

            for msg in encoded:
                msg["Number"] = number
                if smsc:
                    msg["SMSC"] = {"Number": smsc}
                sm.SendSMS(msg)

            self.sms_sent += 1
            logger.info(f"SMS sent to {number} ({len(text)} chars, {len(encoded)} part(s))")
            return f"Sent {len(encoded)} part(s)"

    def get_all_sms(self) -> list[list]:
        """Read all SMS from the modem's memory. Returns list of SMS groups."""
        all_sms = []
        try:
            with self.device() as sm:
                start = True
                while True:
                    try:
                        if start:
                            sms_list = sm.GetNextSMS(Folder=0, Start=True)
                            start = False
                        else:
                            sms_list = sm.GetNextSMS(
                                Folder=0, Location=sms_list[0]["Location"]
                            )
                        all_sms.append(sms_list)
                    except gammu.ERR_EMPTY:
                        break
        except Exception as e:
            logger.warning(f"Error reading SMS: {e}")
        return all_sms

    def delete_sms(self, folder: int, location: int):
        """Delete an SMS from modem memory."""
        with self.device() as sm:
            sm.DeleteSMS(Folder=folder, Location=location)

    def get_signal_quality(self) -> dict:
        """Get current signal quality."""
        info = {}
        try:
            with self.device() as sm:
                sq = sm.GetSignalQuality()
                info["signal_percent"] = sq.get("SignalPercent", -1)
                info["signal_dbm"] = sq.get("SignalStrength", 0)
                info["bit_error_rate"] = sq.get("BitErrorRate", -1)
        except Exception as e:
            logger.warning(f"Error getting signal: {e}")
        return info

    def get_network_info(self) -> dict:
        """Get network registration info."""
        info = {}
        try:
            with self.device() as sm:
                ni = sm.GetNetworkInfo()
                info["network_state"] = ni.get("State", "unknown")
                info["network_code"] = ni.get("NetworkCode", "")
                info["network_name"] = ni.get("NetworkName", "")
                info["gprs"] = ni.get("GPRS", "unknown")
                cid = ni.get("CID", "")
                lac = ni.get("LAC", "")
                if cid or lac:
                    info["cell_info"] = f"LAC {lac}, CID {cid}"
        except Exception as e:
            logger.warning(f"Error getting network info: {e}")
        return info

    def get_battery_status(self) -> dict:
        """Get battery status."""
        info = {}
        try:
            with self.device() as sm:
                bat = sm.GetBatteryCharge()
                info["battery_percent"] = bat.get("BatteryPercent", -1)
                info["charge_state"] = bat.get("ChargeState", "unknown")
        except Exception as e:
            logger.debug(f"Battery info not available: {e}")
        return info

    def capture_modem_info(self):
        """Capture static modem identity info (called once at startup)."""
        try:
            with self.device() as sm:
                mfr = sm.GetManufacturer()
                model = sm.GetModel()
                fw = sm.GetFirmware()
                imei = sm.GetIMEI()

                self.modem_info = {
                    "manufacturer": mfr,
                    "model": f"{model[0]} ({model[1]})" if isinstance(model, (list, tuple)) else str(model),
                    "firmware": fw[0] if isinstance(fw, (list, tuple)) else str(fw),
                    "imei": imei,
                }

                try:
                    sim = sm.GetSIMIMSI()
                    self.modem_info["imsi"] = sim
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Could not capture modem info: {e}")

    def refresh_signal(self, force: bool = False):
        """Refresh signal info (throttled)."""
        now = time.time()
        if not force and (now - self._last_signal_refresh) < settings.signal_refresh:
            return
        self.signal_info = {
            **self.get_signal_quality(),
            **self.get_network_info(),
            **self.get_battery_status(),
        }
        self._last_signal_refresh = now

    def get_ussd(self, code: str) -> str:
        """Send a USSD code and return the response."""
        with self.device() as sm:
            return sm.DialService(code)

    # ─── Receiver thread ─────────────────────────────────────

    def start_receiver(self):
        """Start the background SMS receiver thread."""
        if self._receiver_running:
            return
        self._receiver_running = True
        self._receiver_thread = threading.Thread(
            target=self._receiver_loop, daemon=True, name="sms-receiver"
        )
        self._receiver_thread.start()
        logger.info(f"SMS receiver started (polling every {settings.poll_interval}s)")

    def stop_receiver(self):
        """Stop the receiver thread."""
        self._receiver_running = False

    def _receiver_loop(self):
        """Poll modem for incoming SMS and trigger webhook."""
        Path(settings.received_path).mkdir(parents=True, exist_ok=True)

        while self._receiver_running:
            try:
                sms_groups = self.get_all_sms()
                if sms_groups:
                    grouped = self._group_multipart(sms_groups)
                    for msg in grouped:
                        self._process_incoming(msg)
            except Exception as e:
                logger.warning(f"Receiver error: {e}")
                try:
                    with self.lock:
                        self._reconnect_locked()
                except Exception:
                    pass

            time.sleep(settings.poll_interval)

    def _group_multipart(self, raw_sms_groups: list) -> list[dict]:
        """Group raw gammu message groups into complete messages."""
        result = []

        # Flatten all SMS parts into a single list
        flat = []
        for group in raw_sms_groups:
            for sms in group:
                flat.append(sms)

        if not flat:
            return result

        # LinkSMS expects list of lists
        try:
            linked = gammu.LinkSMS([[s] for s in flat])
        except Exception:
            linked = [[s] for s in flat]

        for group in linked:
            # Flatten nested lists
            parts = []
            for item in group:
                if isinstance(item, list):
                    parts.extend(item)
                else:
                    parts.append(item)

            if not parts:
                continue

            # Try decoding concatenated message
            text = ""
            try:
                decoded = gammu.DecodeSMS(parts)
                if decoded and decoded.get("Entries"):
                    for entry in decoded["Entries"]:
                        if entry.get("Buffer"):
                            text += entry["Buffer"]
            except Exception:
                pass

            # Fallback: read text directly from parts
            if not text:
                for part in parts:
                    if part.get("Text"):
                        text += part["Text"]

            if not text:
                continue

            first = parts[0]
            result.append({
                "text": text,
                "number": first.get("Number", "unknown"),
                "timestamp": first.get("DateTime", datetime.now(timezone.utc)),
                "folder": first.get("Folder", 0),
                "locations": [p.get("Location", 0) for p in parts],
                "parts": len(parts),
                "class": str(first.get("Class", "")),
            })

        return result

    def _process_incoming(self, msg: dict):
        """Store incoming SMS and call webhook."""
        ts = msg["timestamp"]
        if isinstance(ts, datetime):
            ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            ts_file = ts.strftime("%Y%m%d_%H%M%S_%f")
        else:
            ts_str = str(ts)
            ts_file = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        safe_number = "".join(c for c in msg["number"] if c.isdigit() or c == "+")

        # Store as JSON
        store_dir = settings.received_path
        filename = f"{ts_file}_{safe_number}.json"
        filepath = os.path.join(store_dir, filename)

        record = {
            "timestamp": ts_str,
            "number": msg["number"],
            "text": msg["text"],
            "class": msg.get("class", ""),
            "parts": msg["parts"],
        }

        with open(filepath, "w") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

        self.sms_received += 1
        logger.info(f"SMS received from {msg['number']}: {msg['text'][:80]}")

        # Delete from modem
        for loc in msg["locations"]:
            try:
                self.delete_sms(msg["folder"], loc)
            except Exception as e:
                logger.warning(f"Could not delete SMS at location {loc}: {e}")

        # Call webhook
        webhook_url = settings.webhook_url
        if webhook_url:
            payload = {
                "event": "sms_received",
                **record,
            }
            try:
                resp = http_requests.post(
                    webhook_url, json=payload, timeout=10,
                    headers={"Content-Type": "application/json"},
                )
                logger.info(f"Webhook responded HTTP {resp.status_code}")
            except Exception as e:
                logger.error(f"Webhook failed: {e}")


# ─── Global modem instance ───────────────────────────────────

modem = ModemManager(settings.gammu_config)


# ─── App lifecycle ───────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    logger.info("Starting SMS Gateway (python-gammu direct mode)...")
    if modem.connect():
        modem.capture_modem_info()
        modem.refresh_signal(force=True)
        modem.start_receiver()
        logger.info("Gateway ready")
    else:
        logger.error("Could not connect to modem at startup")


@app.on_event("shutdown")
def on_shutdown():
    modem.stop_receiver()
    try:
        with modem.lock:
            if modem._sm:
                modem._sm.Terminate()
    except Exception:
        pass


# ─── Helper: read stored messages ────────────────────────────

def _get_received_messages() -> list[dict]:
    """Read received messages from JSON store."""
    store_dir = settings.received_path
    messages = []

    if not os.path.isdir(store_dir):
        return messages

    for filepath in sorted(glob.glob(os.path.join(store_dir, "*.json")), reverse=True):
        try:
            with open(filepath) as f:
                data = json.load(f)
            data["id"] = Path(filepath).stem
            messages.append(data)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not read {filepath}: {e}")

    return messages


# ─── Endpoints ───────────────────────────────────────────────

@app.post("/api/sms", response_model=SendSMSResponse)
def send_sms(
    req: SendSMSRequest,
    _creds=Depends(verify_credentials),
):
    """Send an SMS message (instant, with multipart support)."""
    try:
        result = modem.send_sms(req.number, req.text, req.smsc)
        return SendSMSResponse(
            status="sent",
            message=result,
            number=req.number,
        )
    except Exception as e:
        modem.sms_failed += 1
        logger.error(f"Failed to send SMS: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send SMS: {e}")


# Backward compat: pajikos style
@app.post("/sms", response_model=SendSMSResponse, include_in_schema=False)
def send_sms_compat(
    req: SendSMSRequest,
    _creds=Depends(verify_credentials),
):
    return send_sms(req, _creds)


@app.get("/api/sms", response_model=list[ReceivedSMS])
def list_received_sms(
    limit: int = 50,
    _creds=Depends(verify_credentials),
):
    """List received SMS messages (newest first)."""
    messages = _get_received_messages()
    result = []
    for m in messages[:limit]:
        result.append(ReceivedSMS(
            id=m.get("id", ""),
            timestamp=m.get("timestamp", ""),
            number=m.get("number", ""),
            text=m.get("text", ""),
            sms_class=m.get("class"),
            parts=m.get("parts", 1),
        ))
    return result


@app.get("/getsms", include_in_schema=False)
def get_sms_compat(_creds=Depends(verify_credentials)):
    return list_received_sms(50, _creds)


@app.get("/api/sms/{msg_id}", response_model=ReceivedSMS)
def get_received_sms(
    msg_id: str,
    _creds=Depends(verify_credentials),
):
    """Get a specific received SMS by ID."""
    messages = _get_received_messages()
    for m in messages:
        if m.get("id") == msg_id:
            return ReceivedSMS(
                id=m["id"],
                timestamp=m.get("timestamp", ""),
                number=m.get("number", ""),
                text=m.get("text", ""),
                sms_class=m.get("class"),
                parts=m.get("parts", 1),
            )
    raise HTTPException(status_code=404, detail="Message not found")


@app.delete("/api/sms/{msg_id}")
def delete_received_sms(
    msg_id: str,
    _creds=Depends(verify_credentials),
):
    """Delete a received SMS from storage."""
    store_dir = settings.received_path
    filepath = os.path.join(store_dir, f"{msg_id}.json")
    if os.path.exists(filepath):
        os.remove(filepath)
        return {"status": "deleted", "id": msg_id}
    raise HTTPException(status_code=404, detail="Message not found")


@app.get("/api/modem/status", response_model=ModemStatus)
def get_modem_status(_creds=Depends(verify_credentials)):
    """Full modem status: identity + signal + counters."""
    modem.refresh_signal()
    mi = modem.modem_info
    si = modem.signal_info
    return ModemStatus(
        modem_active=modem._connected,
        device=os.environ.get("DEVICE", "/dev/mobile"),
        manufacturer=mi.get("manufacturer"),
        model=mi.get("model"),
        firmware=mi.get("firmware"),
        imei=mi.get("imei"),
        imsi=mi.get("imsi"),
        network_state=si.get("network_state"),
        network_name=si.get("network_name"),
        network_code=si.get("network_code"),
        gprs=si.get("gprs"),
        signal_percent=si.get("signal_percent"),
        signal_dbm=si.get("signal_dbm"),
        battery_percent=si.get("battery_percent"),
        sms_sent=modem.sms_sent,
        sms_received=modem.sms_received,
        sms_failed=modem.sms_failed,
    )


@app.get("/api/modem/signal", response_model=SignalInfo)
def get_signal_info(_creds=Depends(verify_credentials)):
    """Real-time signal quality."""
    modem.refresh_signal()
    si = modem.signal_info
    return SignalInfo(
        signal_percent=si.get("signal_percent"),
        signal_dbm=si.get("signal_dbm"),
        battery_percent=si.get("battery_percent"),
        network_name=si.get("network_name"),
        sms_sent=modem.sms_sent,
        sms_received=modem.sms_received,
        sms_failed=modem.sms_failed,
    )


@app.post("/api/ussd")
def send_ussd(
    req: USSDRequest,
    _creds=Depends(verify_credentials),
):
    """Send a USSD code."""
    try:
        result = modem.get_ussd(req.code)
        return {"status": "ok", "response": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"USSD failed: {e}")


@app.get("/api/health", response_model=HealthResponse)
def health_check():
    """Health check (no auth required)."""
    return HealthResponse(
        status="ok" if modem._connected else "degraded",
        modem_active=modem._connected,
        receiver_running=modem._receiver_running,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/api/daemon/logs")
def get_logs(
    lines: int = 50,
    _creds=Depends(verify_credentials),
):
    """Get recent gateway logs."""
    log_file = "/var/log/gammu/gateway.log"
    if not os.path.exists(log_file):
        return {"logs": []}
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), log_file],
            capture_output=True, text=True, timeout=5,
        )
        return {"logs": result.stdout.splitlines()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
