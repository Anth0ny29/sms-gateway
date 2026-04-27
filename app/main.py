"""
Gammu SMS Gateway v4 - Multiprocessing Worker Architecture
===========================================================

Previous versions used threads, which have a fatal flaw: Python threads cannot
be killed. When gammu hangs in a C syscall, the thread holding the /dev/ttyUSB*
file descriptor becomes zombie forever. New workers cannot open the device
because the zombie thread still holds the fd → DEVICEOPENERROR loop.

This version runs the gammu worker in a **separate process** (multiprocessing).
If the worker hangs, we SIGKILL it and the kernel releases all file
descriptors. A fresh worker can then open the device cleanly.

Architecture:
  - Main process: FastAPI + supervisor + receiver thread
  - Worker process: owns gammu.StateMachine, processes commands
  - Communication: two multiprocessing.Queues (commands, responses)
  - Heartbeat: shared multiprocessing.Value updated by worker
  - Supervisor: if no heartbeat for N seconds, SIGKILL + respawn

Guarantees:
  - API never hangs: all operations have timeouts
  - Worker hang → SIGKILL → kernel releases fd → clean respawn
  - Zero zombie state: the OS guarantees cleanup

Fixes v4.1:
  - FIX #1: dispatch() no longer zeroes heartbeat on timeout.
            Only the supervisor decides to kill/respawn.
  - FIX #2: heartbeat_tick() called inside read_sms and refresh_signal
            loops so slow modems don't trigger false-positive respawns.
  - FIX #3: _dispatcher_run() properly handles _startup messages on
            respawn instead of silently discarding them (race condition).
  - FIX #4: worker_respawns counter off-by-one in log fixed.

Fixes v4.2:
  - FIX #5: quick_op_timeout raised 8s→25s and worker_dead_threshold
            raised 30s→90s. After a multi-part send the modem serial port
            stays busy; 8s was too short to wait for GetNextSMS to return.
  - FIX #6: _read_sms_in_flight flag in ModemManager prevents the
            receiver from queuing a new read_sms while the previous one
            is still running. Without this, timed-out commands pile up in
            the worker queue, keeping it stuck far longer than necessary.

Fixes v4.3:
  - FIX #7: SIGALRM-based timeout inside the worker process.
            When gammu hangs in a C syscall (GetNextSMS, GetSignalQuality…)
            Python is suspended and nothing can interrupt it — not
            heartbeats, not queue timeouts, nothing.
            SIGALRM is the ONLY mechanism that can interrupt a blocking C
            call on Linux: the kernel delivers the signal, CPython raises
            TimeoutError, and the worker can exit cleanly.
            Result: recovery time drops from ~95s to ~35s.
  - FIX #8: Exponential backoff in the receiver when gammu operations
            repeatedly fail. Prevents hammering a stuck modem every 3s
            and gives it breathing room to recover after a respawn.
"""

import glob
import json
import logging
import multiprocessing as mp
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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

    poll_interval: int = 3
    signal_refresh: int = 60

    # Timeouts (seconds)
    send_timeout: int = 30
    # FIX #5: was 8s — too short after a multi-part send. The modem serial
    # port stays busy and GetNextSMS needs more time to return.
    quick_op_timeout: int = 25
    init_timeout: int = 20

    # Supervisor
    watchdog_interval: int = 10
    # FIX #5: was 30s — must be well above quick_op_timeout so the
    # supervisor never kills a worker that is running a legitimate slow op.
    worker_dead_threshold: int = 90

    # FIX #7: Maximum seconds a single gammu C-level call may run inside
    # the worker before SIGALRM fires and interrupts it.
    # Must be < quick_op_timeout so the worker exits before dispatch() times out.
    worker_op_timeout: int = 20

settings = Settings()

# ─── Logging ─────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(processName)s] %(message)s"
)
logger = logging.getLogger("sms-gateway")

# ─── Worker process entry point ──────────────────────────────
#
# Must be at module level for multiprocessing to pickle it.
# Runs in a separate process and owns the gammu StateMachine.

def worker_process_main(
    config_path: str,
    command_queue: mp.Queue,
    response_queue: mp.Queue,
    heartbeat: "mp.Value",
    ready_event: "mp.Event",
    shutdown_event: "mp.Event",
):
    """
    Entry point for the worker subprocess.

    Receives commands as dicts: {"id": str, "op": str, "args": dict}
    Sends responses as dicts: {"id": str, "result": any, "error": str|None}
    Updates heartbeat regularly so supervisor knows we're alive.
    Sends "_startup" message when modem is ready, with modem_info payload.
    """
    # In subprocess: reset signal handlers, reconfigure logging
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    # FIX #7: SIGALRM handler — fires when gammu blocks in a C syscall.
    # This is the ONLY way to interrupt a blocking C call from Python on Linux.
    # We raise TimeoutError so the command loop can catch it and exit cleanly.
    def _sigalrm_handler(signum, frame):
        raise TimeoutError("Gammu operation timed out (SIGALRM)")
    signal.signal(signal.SIGALRM, _sigalrm_handler)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(processName)s] %(message)s"
    )
    wlog = logging.getLogger("sms-gateway.worker")

    # Import gammu HERE (in subprocess) to get a clean state
    import gammu

    def heartbeat_tick():
        with heartbeat.get_lock():
            heartbeat.value = time.time()

    # ─── Initialize modem ───
    wlog.info("Worker: initializing modem")
    sm = None
    try:
        sm = gammu.StateMachine()
        sm.ReadConfig(Filename=config_path)
        sm.Init()
        heartbeat_tick()
        wlog.info("Worker: modem connected")
    except Exception as e:
        wlog.error(f"Worker: init failed: {e}")
        try:
            response_queue.put({
                "id": "_startup",
                "result": None,
                "error": f"Init failed: {e}",
            })
        except Exception:
            pass
        return

    # Capture identity
    modem_info = {}
    try:
        mfr = sm.GetManufacturer()
        model = sm.GetModel()
        fw = sm.GetFirmware()
        imei = sm.GetIMEI()
        modem_info = {
            "manufacturer": mfr,
            "model": (
                f"{model[0]} ({model[1]})"
                if isinstance(model, (list, tuple)) else str(model)
            ),
            "firmware": (
                fw[0] if isinstance(fw, (list, tuple)) else str(fw)
            ),
            "imei": imei,
        }
        try:
            modem_info["imsi"] = sm.GetSIMIMSI()
        except Exception:
            pass
    except Exception as e:
        wlog.warning(f"Worker: could not capture identity: {e}")

    # Signal ready
    try:
        response_queue.put({
            "id": "_startup",
            "result": {"modem_info": modem_info},
            "error": None,
        })
        ready_event.set()
    except Exception:
        pass

    heartbeat_tick()

    # ─── Command loop ───
    while not shutdown_event.is_set():
        heartbeat_tick()

        try:
            cmd = command_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        except Exception as e:
            wlog.error(f"Worker: queue error: {e}")
            break

        if cmd is None or cmd.get("op") == "_shutdown":
            break

        cmd_id = cmd.get("id", "?")
        op = cmd.get("op", "")
        args = cmd.get("args", {})

        try:
            # FIX #7: arm SIGALRM before every gammu call.
            # If gammu hangs in C code beyond worker_op_timeout seconds,
            # the kernel delivers SIGALRM → _sigalrm_handler raises
            # TimeoutError → we catch it below and exit for a clean respawn.
            signal.alarm(settings.worker_op_timeout)
            try:
                result = _execute_command(sm, op, args, gammu, heartbeat_tick)
            finally:
                signal.alarm(0)  # Always disarm, even on exception

            response_queue.put({
                "id": cmd_id,
                "result": result,
                "error": None,
            })
        except TimeoutError as e:
            # FIX #7: gammu was stuck in a C syscall — SIGALRM fired.
            # Send the error back so dispatch() doesn't hang waiting,
            # then exit so the supervisor can respawn us cleanly.
            wlog.warning(f"Worker: SIGALRM on '{op}': {e}")
            try:
                response_queue.put({
                    "id": cmd_id,
                    "result": None,
                    "error": f"worker SIGALRM timeout: {op}",
                })
            except Exception:
                pass
            break
        except gammu.ERR_TIMEOUT as e:
            wlog.warning(f"Worker: gammu timeout on '{op}': {e}")
            response_queue.put({
                "id": cmd_id,
                "result": None,
                "error": f"gammu timeout: {e}",
            })
            # Bail out on timeout so supervisor respawns us
            break
        except Exception as e:
            response_queue.put({
                "id": cmd_id,
                "result": None,
                "error": f"{type(e).__name__}: {e}",
            })

        heartbeat_tick()

    # Cleanup
    try:
        if sm:
            sm.Terminate()
    except Exception:
        pass
    wlog.info("Worker: exiting")


# FIX #2: heartbeat_tick added as parameter (default None for safety).
# Called at each iteration of the GetNextSMS loop and between each
# gammu call in refresh_signal so the supervisor never sees a stale
# heartbeat during a legitimate slow operation.
def _execute_command(sm, op: str, args: dict, gammu, heartbeat_tick=None) -> Any:
    """Execute a single command in the worker subprocess."""

    def _tick():
        if heartbeat_tick is not None:
            heartbeat_tick()

    if op == "send_sms":
        number = args["number"]
        text = args["text"]
        smsc = args.get("smsc")
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
        return {"parts": len(encoded)}

    elif op == "read_sms":
        all_sms = []
        start = True
        while True:
            _tick()  # FIX #2: tick at each iteration — modem may be slow
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

        # Serialize + process in worker so main only gets picklable dicts
        return _serialize_and_group_sms(all_sms, gammu)

    elif op == "delete_sms":
        sm.DeleteSMS(Folder=args["folder"], Location=args["location"])
        return True

    elif op == "refresh_signal":
        info = {}
        try:
            sq = sm.GetSignalQuality()
            info["signal_percent"] = sq.get("SignalPercent", -1)
            info["signal_dbm"] = sq.get("SignalStrength", 0)
        except Exception:
            pass
        _tick()  # FIX #2: tick between each gammu call
        try:
            ni = sm.GetNetworkInfo()
            info["network_state"] = ni.get("State", "unknown")
            info["network_code"] = ni.get("NetworkCode", "")
            info["network_name"] = ni.get("NetworkName", "")
            info["gprs"] = ni.get("GPRS", "unknown")
        except Exception:
            pass
        _tick()  # FIX #2: tick between each gammu call
        try:
            bat = sm.GetBatteryCharge()
            info["battery_percent"] = bat.get("BatteryPercent", -1)
        except Exception:
            pass
        return info

    elif op == "ussd":
        return sm.DialService(args["code"])

    else:
        raise ValueError(f"Unknown op: {op}")


def _serialize_and_group_sms(raw_groups: list, gammu) -> list:
    """
    Group multipart and serialize to plain dicts (picklable).
    Returns list of messages ready to process in main.
    """
    result = []

    flat = []
    for group in raw_groups:
        for sms in group:
            flat.append(sms)

    if not flat:
        return result

    try:
        linked = gammu.LinkSMS([[s] for s in flat])
    except Exception:
        linked = [[s] for s in flat]

    for group in linked:
        parts = []
        for item in group:
            if isinstance(item, list):
                parts.extend(item)
            else:
                parts.append(item)

        if not parts:
            continue

        text = ""
        try:
            decoded = gammu.DecodeSMS(parts)
            if decoded and decoded.get("Entries"):
                for entry in decoded["Entries"]:
                    if entry.get("Buffer"):
                        text += entry["Buffer"]
        except Exception:
            pass

        if not text:
            for part in parts:
                if part.get("Text"):
                    text += part["Text"]

        if not text:
            continue

        first = parts[0]
        dt = first.get("DateTime")
        ts_iso = (
            dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            if isinstance(dt, datetime) else ""
        )

        result.append({
            "text": text,
            "number": first.get("Number", "unknown"),
            "timestamp": ts_iso,
            "folder": first.get("Folder", 0),
            "locations": [p.get("Location", 0) for p in parts],
            "parts": len(parts),
            "class": str(first.get("Class", "")),
        })

    return result


# ─── FastAPI setup ───────────────────────────────────────────

app = FastAPI(
    title="Gammu SMS Gateway",
    description="Resilient SMS REST API via multiprocessing worker",
    version="4.3.0",
)

security = HTTPBasic()

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
    number: str = Field(...)
    text: str = Field(...)
    smsc: Optional[str] = Field(None)

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
    worker_respawns: int = 0

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
    worker_respawns: int
    timestamp: str

class USSDRequest(BaseModel):
    code: str = Field(...)

# ─── Modem Manager (main process side) ───────────────────────

class ModemManager:
    """
    Manages the worker subprocess.
    Dispatches commands via mp.Queue and correlates responses by ID.
    Supervisor kills and respawns the worker on hang.
    """

    def __init__(self, config_path: str):
        self.config_path = config_path

        # Multiprocessing primitives (created on start())
        self._command_queue: Optional[mp.Queue] = None
        self._response_queue: Optional[mp.Queue] = None
        self._heartbeat: Optional[Any] = None  # mp.Value
        self._ready_event: Optional[Any] = None  # mp.Event
        self._shutdown_event: Optional[Any] = None  # mp.Event
        self._worker: Optional[mp.Process] = None

        # Response correlation
        self._pending: dict[str, threading.Event] = {}
        self._results: dict[str, dict] = {}
        self._pending_lock = threading.Lock()

        # Supervisor / receiver threads
        self._dispatcher_thread: Optional[threading.Thread] = None
        self._supervisor_thread: Optional[threading.Thread] = None
        self._receiver_thread: Optional[threading.Thread] = None
        self._running = False

        # State
        self._connected = False
        self.modem_info: dict = {}
        self.signal_info: dict = {}
        self._last_signal_refresh = 0.0

        # Counters
        self.sms_sent = 0
        self.sms_received = 0
        self.sms_failed = 0
        self.worker_respawns = 0

        # FIX #6: prevents receiver from queuing a new read_sms while the
        # previous one is still in flight. Avoids command pile-up in the
        # worker queue when the modem is slow.
        self._read_sms_in_flight = False
        self._read_sms_lock = threading.Lock()

        # FIX #8: consecutive failure counter for receiver backoff.
        # When the modem is stuck, we don't want to retry every 3s.
        self._receiver_failures = 0

    # ─── Worker lifecycle ────────────────────────────────────

    def _spawn_worker(self) -> bool:
        """Spawn a fresh worker process. Returns True if connected."""
        # Create fresh queues each time (old ones may have stale data)
        ctx = mp.get_context("fork")  # Linux-only: fast
        self._command_queue = ctx.Queue()
        self._response_queue = ctx.Queue()
        self._heartbeat = ctx.Value("d", time.time())
        self._ready_event = ctx.Event()
        self._shutdown_event = ctx.Event()

        self._worker = ctx.Process(
            target=worker_process_main,
            args=(
                self.config_path,
                self._command_queue,
                self._response_queue,
                self._heartbeat,
                self._ready_event,
                self._shutdown_event,
            ),
            name=f"modem-worker-{self.worker_respawns + 1}",
            daemon=True,
        )
        self._worker.start()

        # Wait for startup signal
        if not self._ready_event.wait(timeout=settings.init_timeout):
            logger.error(
                f"Worker failed to initialize in {settings.init_timeout}s"
            )
            self._kill_worker()
            return False

        # FIX #3: On initial spawn the dispatcher isn't running yet so
        # we can safely read _startup from the queue here.
        # On respawn the dispatcher may have already consumed it and
        # updated modem_info directly (see _dispatcher_run). Either way
        # we end up with fresh modem_info — the queue.Empty fallback is
        # harmless.
        try:
            startup_resp = self._response_queue.get(timeout=2)
            if startup_resp.get("error"):
                logger.error(f"Worker init error: {startup_resp['error']}")
                self._kill_worker()
                return False
            result = startup_resp.get("result") or {}
            if result.get("modem_info"):
                self.modem_info = result["modem_info"]
        except queue.Empty:
            # On respawn the dispatcher may have already processed _startup.
            # modem_info was updated there — this is not an error.
            logger.debug("Worker started — _startup already consumed by dispatcher")

        self._connected = True
        logger.info(f"Worker PID {self._worker.pid} online")
        return True

    def _kill_worker(self):
        """Brutally kill the worker. Kernel frees all fds."""
        if self._worker is None:
            return

        self._connected = False

        # Try graceful shutdown first
        try:
            if self._shutdown_event:
                self._shutdown_event.set()
        except Exception:
            pass

        # Give it 2 seconds to exit gracefully
        self._worker.join(timeout=2)

        # SIGTERM
        if self._worker.is_alive():
            try:
                self._worker.terminate()
                self._worker.join(timeout=2)
            except Exception:
                pass

        # SIGKILL - guaranteed to release fds
        if self._worker.is_alive():
            try:
                self._worker.kill()
                self._worker.join(timeout=5)
            except Exception:
                pass

        try:
            self._worker.close()
        except Exception:
            pass

        self._worker = None

        # Reset in-flight flag so the receiver doesn't stay permanently
        # blocked after a respawn (the old command is gone with the worker).
        with self._read_sms_lock:
            self._read_sms_in_flight = False
        # FIX #8: reset backoff counter on respawn — new worker is fresh.
        self._receiver_failures = 0

        # Fail all pending commands
        with self._pending_lock:
            for cmd_id, event in list(self._pending.items()):
                self._results[cmd_id] = {
                    "result": None,
                    "error": "Worker killed",
                }
                event.set()
            self._pending.clear()

    def _respawn_worker(self):
        """Kill and respawn."""
        self.worker_respawns += 1
        # FIX #4: off-by-one — worker_respawns was already incremented above.
        logger.warning(
            f"Respawning worker (#{self.worker_respawns})"
        )
        self._kill_worker()

        # Small pause to let the kernel fully release fds
        time.sleep(1)

        if self._spawn_worker():
            logger.info(
                f"Worker respawned successfully "
                f"(total respawns: {self.worker_respawns})"
            )
        else:
            logger.error("Worker respawn failed")

    # ─── Response dispatcher ─────────────────────────────────

    def _dispatcher_run(self):
        """
        Read responses from the worker and resolve pending Futures.
        Runs as a thread in the main process.

        FIX #3: _startup messages are now handled here instead of being
        silently discarded. On respawn the dispatcher is already running
        and may grab _startup before _spawn_worker's queue.get(timeout=2).
        We update modem_info directly so no data is ever lost.
        """
        while self._running:
            q = self._response_queue
            if q is None:
                time.sleep(0.5)
                continue

            try:
                resp = q.get(timeout=0.5)
            except queue.Empty:
                continue
            except (EOFError, BrokenPipeError, OSError):
                # Worker was killed
                time.sleep(0.5)
                continue

            cmd_id = resp.get("id")

            # FIX #3: handle _startup instead of discarding it.
            # This fires during respawn when the dispatcher is already
            # running and wins the race against _spawn_worker.get().
            if cmd_id == "_startup":
                if resp.get("error"):
                    logger.error(
                        f"Dispatcher: worker startup error: {resp['error']}"
                    )
                else:
                    result = resp.get("result") or {}
                    if result.get("modem_info"):
                        self.modem_info = result["modem_info"]
                        logger.info(
                            "Dispatcher: modem_info updated from _startup"
                        )
                    self._connected = True
                continue

            if cmd_id is None:
                continue

            with self._pending_lock:
                event = self._pending.pop(cmd_id, None)
                if event is not None:
                    self._results[cmd_id] = resp
                    event.set()

    # ─── Supervisor ──────────────────────────────────────────

    def _supervisor_run(self):
        """Monitor worker heartbeat and respawn if dead/stuck."""
        while self._running:
            time.sleep(settings.watchdog_interval)

            if not self._running:
                break

            # Worker alive?
            alive = self._worker is not None and self._worker.is_alive()

            # Heartbeat fresh?
            if self._heartbeat is not None:
                try:
                    hb = self._heartbeat.value
                except Exception:
                    hb = 0
                fresh = (time.time() - hb) < settings.worker_dead_threshold
            else:
                fresh = False

            if alive and fresh:
                continue

            if not alive:
                logger.warning("Supervisor: worker process dead")
            else:
                inactive = int(time.time() - self._heartbeat.value)
                logger.warning(
                    f"Supervisor: worker stuck "
                    f"({inactive}s no heartbeat)"
                )

            self._respawn_worker()

    # ─── Command dispatch ────────────────────────────────────

    def dispatch(self, op: str, args: dict, timeout: float) -> Any:
        """Send a command and wait for the response."""
        if self._worker is None or not self._worker.is_alive():
            raise RuntimeError("Worker not available")

        cmd_id = str(uuid.uuid4())
        event = threading.Event()

        with self._pending_lock:
            self._pending[cmd_id] = event

        try:
            self._command_queue.put({
                "id": cmd_id,
                "op": op,
                "args": args,
            })
        except Exception as e:
            with self._pending_lock:
                self._pending.pop(cmd_id, None)
            raise RuntimeError(f"Could not queue command: {e}")

        if not event.wait(timeout=timeout):
            # Timeout — clean up pending entry and raise.
            with self._pending_lock:
                self._pending.pop(cmd_id, None)
            logger.warning(
                f"Command '{op}' timed out after {timeout}s"
            )
            # FIX #1: do NOT zero the heartbeat here.
            # The old code did `heartbeat.value = 0` which triggered an
            # immediate supervisor respawn even when the worker was just
            # slow (not actually stuck). The supervisor already monitors
            # the real heartbeat — if the worker is truly hung in a C
            # syscall it will stop ticking naturally and the supervisor
            # will respawn it after worker_dead_threshold seconds.
            raise RuntimeError(f"Command timeout: {op}")

        with self._pending_lock:
            resp = self._results.pop(cmd_id, None)

        if resp is None:
            raise RuntimeError("No response received")

        if resp.get("error"):
            raise RuntimeError(resp["error"])

        return resp.get("result")

    # ─── Public API ──────────────────────────────────────────

    def send_sms(self, number: str, text: str, smsc: Optional[str] = None) -> str:
        result = self.dispatch(
            "send_sms",
            {"number": number, "text": text, "smsc": smsc},
            timeout=settings.send_timeout,
        )
        parts = result.get("parts", 1)
        self.sms_sent += 1
        logger.info(
            f"SMS sent to {number} ({len(text)} chars, {parts} part(s))"
        )
        return f"Sent {parts} part(s)"

    def send_ussd(self, code: str) -> str:
        return self.dispatch(
            "ussd", {"code": code}, timeout=settings.send_timeout
        )

    # ─── Start / stop ────────────────────────────────────────

    def start(self):
        self._running = True

        # Start worker first
        if not self._spawn_worker():
            logger.error("Could not spawn initial worker")
            # Still start supervisor - it will retry

        # Dispatcher thread (reads responses)
        self._dispatcher_thread = threading.Thread(
            target=self._dispatcher_run,
            daemon=True,
            name="dispatcher",
        )
        self._dispatcher_thread.start()

        # Supervisor thread
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_run,
            daemon=True,
            name="supervisor",
        )
        self._supervisor_thread.start()

        # Receiver thread
        self._receiver_thread = threading.Thread(
            target=self._receiver_loop,
            daemon=True,
            name="receiver",
        )
        self._receiver_thread.start()

        logger.info("Gateway started")

    def stop(self):
        self._running = False
        self._kill_worker()

    # ─── Receiver (main process thread) ──────────────────────

    def _receiver_loop(self):
        """
        Poll for SMS and refresh signal via the worker.
        All gammu work happens in the subprocess.
        """
        Path(settings.received_path).mkdir(parents=True, exist_ok=True)

        while self._running:
            if not self._connected or self._worker is None or not self._worker.is_alive():
                time.sleep(1)
                continue

            # Poll SMS
            # FIX #6: skip if a read_sms is already in flight — we must
            # not pile up commands in the worker queue when the modem is
            # slow. A timed-out dispatch() doesn't mean the worker is
            # done; it may still be running GetNextSMS in C code.
            with self._read_sms_lock:
                already_running = self._read_sms_in_flight
                if not already_running:
                    self._read_sms_in_flight = True

            if already_running:
                logger.debug("Receiver: read_sms still in flight, skipping poll")
            else:
                try:
                    messages = self.dispatch(
                        "read_sms", {},
                        timeout=settings.quick_op_timeout,
                    )
                    if messages:
                        for msg in messages:
                            self._process_incoming(msg)
                    # FIX #8: success — reset failure counter and restore
                    # normal poll interval.
                    self._receiver_failures = 0
                except Exception as e:
                    logger.debug(f"Receiver: read_sms failed: {e}")
                    # FIX #8: count failure for backoff calculation below.
                    self._receiver_failures += 1
                finally:
                    with self._read_sms_lock:
                        self._read_sms_in_flight = False

            # Refresh signal — but only if the modem didn't just hang on
            # read_sms (FIX #8: _receiver_failures > 0 means it failed this
            # cycle; no point hitting the modem again immediately).
            if (
                self._receiver_failures == 0
                and time.time() - self._last_signal_refresh >= settings.signal_refresh
            ):
                try:
                    info = self.dispatch(
                        "refresh_signal", {},
                        timeout=settings.quick_op_timeout,
                    )
                    if info:
                        self.signal_info = info
                    self._last_signal_refresh = time.time()
                except Exception as e:
                    logger.debug(f"Receiver: signal failed: {e}")

            # FIX #8: exponential backoff — when the modem is stuck, back
            # off polling to avoid hammering it.
            # 0 failures → normal poll_interval (3s)
            # 1 failure   → 10s
            # 2 failures  → 20s
            # 3+ failures → 30s (capped)
            if self._receiver_failures == 0:
                sleep_for = settings.poll_interval
            else:
                sleep_for = min(self._receiver_failures * 10, 30)
                logger.debug(
                    f"Receiver: backing off {sleep_for}s "
                    f"({self._receiver_failures} consecutive failure(s))"
                )

            # Sleep interruptibly
            end = time.time() + sleep_for
            while time.time() < end and self._running:
                time.sleep(0.2)

        logger.info("Receiver exiting")

    def _process_incoming(self, msg: dict):
        """Store SMS, delete from modem, call webhook."""
        ts_iso = msg.get("timestamp") or ""
        if not ts_iso:
            ts_iso = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        ts_file = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_number = "".join(
            c for c in msg["number"] if c.isdigit() or c == "+"
        )

        filename = f"{ts_file}_{safe_number}.json"
        filepath = os.path.join(settings.received_path, filename)

        record = {
            "timestamp": ts_iso,
            "number": msg["number"],
            "text": msg["text"],
            "class": msg.get("class", ""),
            "parts": msg["parts"],
        }

        try:
            with open(filepath, "w") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Could not write SMS to disk: {e}")
            return

        self.sms_received += 1
        logger.info(
            f"SMS received from {msg['number']}: {msg['text'][:80]}"
        )

        # Delete from modem via worker
        for loc in msg["locations"]:
            try:
                self.dispatch(
                    "delete_sms",
                    {"folder": msg["folder"], "location": loc},
                    timeout=settings.quick_op_timeout,
                )
            except Exception as e:
                logger.warning(f"Could not delete SMS at {loc}: {e}")

        # Webhook
        if settings.webhook_url:
            payload = {"event": "sms_received", **record}
            try:
                resp = http_requests.post(
                    settings.webhook_url,
                    json=payload,
                    timeout=10,
                    headers={"Content-Type": "application/json"},
                )
                logger.info(f"Webhook responded HTTP {resp.status_code}")
            except Exception as e:
                logger.error(f"Webhook failed: {e}")


# ─── Global instance ─────────────────────────────────────────

modem = ModemManager(settings.gammu_config)

# ─── App lifecycle ───────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    logger.info("Starting SMS Gateway v4.3 (multiprocessing worker)...")
    modem.start()
    logger.info("Gateway ready")


@app.on_event("shutdown")
def on_shutdown():
    logger.info("Shutting down gateway...")
    modem.stop()


# ─── Stored messages ─────────────────────────────────────────

def _get_received_messages() -> list[dict]:
    messages = []
    if not os.path.isdir(settings.received_path):
        return messages

    for filepath in sorted(
        glob.glob(os.path.join(settings.received_path, "*.json")),
        reverse=True,
    ):
        try:
            with open(filepath) as f:
                data = json.load(f)
            data["id"] = Path(filepath).stem
            messages.append(data)
        except (json.JSONDecodeError, IOError):
            pass
    return messages


# ─── Endpoints ───────────────────────────────────────────────

@app.post("/api/sms", response_model=SendSMSResponse)
def send_sms(req: SendSMSRequest, _creds=Depends(verify_credentials)):
    try:
        result = modem.send_sms(req.number, req.text, req.smsc)
        return SendSMSResponse(
            status="sent", message=result, number=req.number
        )
    except Exception as e:
        modem.sms_failed += 1
        logger.error(f"Failed to send SMS: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to send SMS: {e}"
        )


@app.post("/sms", response_model=SendSMSResponse, include_in_schema=False)
def send_sms_compat(req: SendSMSRequest, _creds=Depends(verify_credentials)):
    return send_sms(req, _creds)


@app.get("/api/sms", response_model=list[ReceivedSMS])
def list_received_sms(limit: int = 50, _creds=Depends(verify_credentials)):
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
def get_received_sms(msg_id: str, _creds=Depends(verify_credentials)):
    for m in _get_received_messages():
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
def delete_received_sms(msg_id: str, _creds=Depends(verify_credentials)):
    filepath = os.path.join(settings.received_path, f"{msg_id}.json")
    if os.path.exists(filepath):
        os.remove(filepath)
        return {"status": "deleted", "id": msg_id}
    raise HTTPException(status_code=404, detail="Message not found")


@app.get("/api/modem/status", response_model=ModemStatus)
def get_modem_status(_creds=Depends(verify_credentials)):
    """Cached modem info - instant."""
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
        worker_respawns=modem.worker_respawns,
    )


@app.get("/api/modem/signal", response_model=SignalInfo)
def get_signal_info(_creds=Depends(verify_credentials)):
    """Cached signal - instant."""
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
def send_ussd(req: USSDRequest, _creds=Depends(verify_credentials)):
    try:
        result = modem.send_ussd(req.code)
        return {"status": "ok", "response": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"USSD failed: {e}")


@app.get("/api/health", response_model=HealthResponse)
def health_check():
    worker_alive = (
        modem._worker is not None and modem._worker.is_alive()
    )
    receiver_alive = (
        modem._receiver_thread is not None
        and modem._receiver_thread.is_alive()
    )
    return HealthResponse(
        status="ok" if (modem._connected and worker_alive and receiver_alive) else "degraded",
        modem_active=modem._connected,
        receiver_running=receiver_alive,
        worker_respawns=modem.worker_respawns,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/api/daemon/logs")
def get_logs(lines: int = 50, _creds=Depends(verify_credentials)):
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
