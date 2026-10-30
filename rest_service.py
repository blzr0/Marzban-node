import asyncio
import json
import os
import threading
import time
from collections import deque
from typing import Optional
from uuid import UUID, uuid4

from fastapi import (APIRouter, Body, FastAPI, HTTPException, Request,
                     WebSocket, status)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from config import (NODE_STATUS_SOCKET_PATH, PERSIST_CONFIG,
                    PERSISTED_CONFIG_FILE, SERVICE_PROTOCOL, XRAY_ASSETS_PATH,
                    XRAY_EXECUTABLE_PATH)
from logger import logger
from status_socket import serve_status_over_unix_socket
from xray import XRayConfig, XRayCore

app = FastAPI()

# How long the panel may stay silent (it pings every ~10s) before the node
# restarts a dead Xray by itself. While the panel is around it restarts the
# core with fresh users; the node only has the users of the last start.
PANEL_GRACE_SECONDS = 30
# Watchdog gives up after this many restarts within a minute (broken config)
WATCHDOG_MAX_RESTARTS = 5


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    details = {}
    for error in exc.errors():
        details[error["loc"][-1]] = error.get("msg")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=jsonable_encoder({"detail": details}),
    )


class Service(object):
    def __init__(self):
        self.router = APIRouter()

        self.connected = False
        self.client_ip = None
        self.session_id = None
        self.core = XRayCore(
            executable_path=XRAY_EXECUTABLE_PATH,
            assets_path=XRAY_ASSETS_PATH
        )
        self.core_version = self.core.get_version()
        self.config = None

        # Opaque fingerprint of the config the running Xray was started with
        # (sent by the panel, users excluded), and the panel IP it was started
        # for. Lets a reconnecting panel attach to a healthy core instead of
        # restarting it - see response().
        self.config_fingerprint = None
        self.started_peer_ip = None

        # serializes everything that starts/stops Xray (panel requests run in
        # a thread pool, the watchdog in its own thread)
        self._core_lock = threading.Lock()
        self._panel_seen = None
        # {"config", "peer_ip", "fingerprint"} of the last panel start
        self._saved = None
        self._watchdog_starts = deque(maxlen=WATCHDOG_MAX_RESTARTS)
        self._watchdog_gave_up = False

        if SERVICE_PROTOCOL == "rest":
            serve_status_over_unix_socket(NODE_STATUS_SOCKET_PATH, self.status)

            if PERSIST_CONFIG:
                self._saved = self._load_saved_config()
                if self._saved:
                    with self._core_lock:
                        self._start_saved("autostart")
                threading.Thread(target=self._watchdog, daemon=True).start()

        self.router.add_api_route("/", self.base, methods=["POST"])
        self.router.add_api_route("/ping", self.ping, methods=["POST"])
        self.router.add_api_route("/connect", self.connect, methods=["POST"])
        self.router.add_api_route("/disconnect", self.disconnect, methods=["POST"])
        self.router.add_api_route("/start", self.start, methods=["POST"])
        self.router.add_api_route("/stop", self.stop, methods=["POST"])
        self.router.add_api_route("/restart", self.restart, methods=["POST"])
        self.router.add_api_route("/status", self.status, methods=["GET"])

        self.router.add_websocket_route("/logs", self.logs)

    def match_session_id(self, session_id: UUID):
        if session_id != self.session_id:
            raise HTTPException(
                status_code=403,
                detail="Session ID mismatch."
            )
        self._panel_seen = time.time()
        return True

    def attachable_fingerprint(self):
        """The running config's fingerprint, but only if the current client
        could use the core as-is. XRayConfig bakes the panel's IP into the
        API routing rule at start, so a panel connecting from another IP
        can't reach Xray's API without a restart - report nothing then, and
        the panel falls back to a normal restart.
        """
        if not self.core.started or not self.config_fingerprint:
            return None
        if self.client_ip != self.started_peer_ip:
            return None
        return self.config_fingerprint

    def response(self, **kwargs):
        return {
            "connected": self.connected,
            "started": self.core.started,
            "core_version": self.core_version,
            "config_fingerprint": self.attachable_fingerprint(),
            **kwargs
        }

    def base(self):
        return self.response()

    def status(self):
        """Diagnostic snapshot of the running Xray process - no session_id
        check, since this is meant to also work for a locally-run CLI that
        has no session with the panel (see cli.py). mTLS on this whole app
        is the only auth gate, same as every other route.
        """
        return self.core.get_status()

    def connect(self, request: Request):
        self.session_id = uuid4()
        self.client_ip = request.client.host

        if self.connected:
            # Only control moves to the new client - the core keeps running.
            # A reconnecting panel (restart, network blip) decides via
            # config_fingerprint whether it needs a restart at all.
            logger.warning(
                f'New connection from {self.client_ip}, Core control access was taken away from previous client.')

        self.connected = True
        self._panel_seen = time.time()
        logger.info(f'{self.client_ip} connected, Session ID = "{self.session_id}".')

        return self.response(
            session_id=self.session_id
        )

    def disconnect(self):
        if self.connected:
            logger.info(f'{self.client_ip} disconnected, Session ID = "{self.session_id}".')

        self.session_id = None
        self.client_ip = None
        self.connected = False

        #if self.core.started:
        #    try:
        #        self.core.stop()
        #    except RuntimeError:
        #        pass

        return self.response()

    def ping(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)
        return {}

    def _wait_for_core_start(self, logs):
        """Wait up to 3 seconds for Xray's "started" line, returns the last
        log line seen (the error detail if Xray didn't come up).
        """
        end_time = time.time() + 3
        last_log = ''
        while time.time() < end_time:
            while logs:
                log = logs.popleft()
                if log:
                    last_log = log
                if f'Xray {self.core_version} started' in log:
                    return last_log
            time.sleep(0.1)
        return last_log

    def _run_core(self, config: str, config_fingerprint: Optional[str], action: str):
        with self._core_lock:
            return self._run_core_locked(config, config_fingerprint, action)

    def _run_core_locked(self, raw_config: str, config_fingerprint: Optional[str], action: str):
        self.config_fingerprint = None

        try:
            config = XRayConfig(raw_config, self.client_ip)
        except json.decoder.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "config": f'Failed to decode config: {exc}'
                }
            )

        try:
            with self.core.get_logs() as logs:
                # get_logs() is pre-filled with history - drop it so an old
                # "started" line can't pass for this launch
                logs.clear()
                if action == "restart":
                    self.core.restart(config, reason="panel_requested")
                else:
                    self.core.start(config)
                last_log = self._wait_for_core_start(logs)

        except Exception as exc:
            logger.error(f"Failed to {action} core: {exc}")
            self.core.last_error = str(exc)
            raise HTTPException(
                status_code=503,
                detail=str(exc)
            )

        if not self.core.started:
            raise HTTPException(
                status_code=503,
                detail=last_log
            )

        self.config_fingerprint = config_fingerprint
        self.started_peer_ip = self.client_ip
        self._save_config(raw_config, self.client_ip, config_fingerprint)
        return self.response()

    def _load_saved_config(self):
        try:
            with open(PERSISTED_CONFIG_FILE) as f:
                saved = json.load(f)
            saved["config"], saved["peer_ip"]
            return saved
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning(f"Ignoring unreadable saved config {PERSISTED_CONFIG_FILE}: {exc}")
            return None

    def _save_config(self, raw_config: str, peer_ip: str, config_fingerprint: Optional[str]):
        if not PERSIST_CONFIG:
            return

        self._saved = {"config": raw_config, "peer_ip": peer_ip, "fingerprint": config_fingerprint}
        self._watchdog_starts.clear()
        self._watchdog_gave_up = False

        tmp = PERSISTED_CONFIG_FILE + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self._saved, f)
            os.replace(tmp, PERSISTED_CONFIG_FILE)
        except OSError as exc:
            logger.warning(f"Failed to save config to {PERSISTED_CONFIG_FILE}: {exc}")

    def _forget_config(self):
        self._saved = None
        try:
            os.remove(PERSISTED_CONFIG_FILE)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"Failed to remove {PERSISTED_CONFIG_FILE}: {exc}")

    def _start_saved(self, reason: str):
        """Start Xray from the last panel config, without the panel. Caller
        holds _core_lock. Restores the panel's fingerprint and IP, so the
        panel attaches on reconnect and syncs the users changed meanwhile.
        """
        saved = self._saved
        crash_error = self.core.last_error
        logger.warning(f"Starting Xray from the saved config ({reason})")
        try:
            config = XRayConfig(saved["config"], saved["peer_ip"])
            with self.core.get_logs() as logs:
                logs.clear()
                self.core.start(config)
                last_log = self._wait_for_core_start(logs)
        except Exception as exc:
            logger.error(f"Failed to start Xray from the saved config: {exc}")
            self.core.last_error = str(exc)
            return

        if not self.core.started:
            logger.error(f"Failed to start Xray from the saved config: {last_log}")
            return

        self.core.last_restart_reason = reason
        # keep why it went down visible in `cli.py status`
        self.core.last_error = crash_error
        self.config_fingerprint = saved.get("fingerprint")
        self.started_peer_ip = saved["peer_ip"]

    def _watchdog(self):
        while True:
            time.sleep(2)
            try:
                self._watchdog_check()
            except Exception as exc:
                logger.error(f"Watchdog: {exc}")

    def _watchdog_check(self):
        if self.core.started or not self._saved or self._watchdog_gave_up:
            return
        if self._panel_seen and time.time() - self._panel_seen < PANEL_GRACE_SECONDS:
            return

        with self._core_lock:
            if self.core.started or not self._saved:
                return

            now = time.time()
            if len(self._watchdog_starts) == WATCHDOG_MAX_RESTARTS and now - self._watchdog_starts[0] < 60:
                self._watchdog_gave_up = True
                logger.error(f"Watchdog: Xray failed {WATCHDOG_MAX_RESTARTS} times within a minute, "
                             "not restarting it until the panel starts it again")
                return
            self._watchdog_starts.append(now)
            self._start_saved("watchdog")

    def start(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True),
              config_fingerprint: Optional[str] = Body(None, embed=True)):
        self.match_session_id(session_id)
        return self._run_core(config, config_fingerprint, "start")

    def stop(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)

        with self._core_lock:
            try:
                self.core.stop()

            except RuntimeError:
                pass

            # stopped on purpose - don't bring it back on the next boot
            self._forget_config()

        self.config_fingerprint = None
        return self.response()

    def restart(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True),
              config_fingerprint: Optional[str] = Body(None, embed=True)):
        self.match_session_id(session_id)
        return self._run_core(config, config_fingerprint, "restart")

    async def logs(self, websocket: WebSocket):
        session_id = websocket.query_params.get('session_id')
        interval = websocket.query_params.get('interval')

        try:
            session_id = UUID(session_id)
            if session_id != self.session_id:
                return await websocket.close(reason="Session ID mismatch.", code=4403)

        except ValueError:
            return await websocket.close(reason="session_id should be a valid UUID.", code=4400)

        if interval:
            try:
                interval = float(interval)

            except ValueError:
                return await websocket.close(reason="Invalid interval value.", code=4400)

            if interval > 10:
                return await websocket.close(reason="Interval must be more than 0 and at most 10 seconds.", code=4400)

        await websocket.accept()

        cache = ''
        last_sent_ts = 0
        with self.core.get_logs() as logs:
            while session_id == self.session_id:
                if interval and time.time() - last_sent_ts >= interval and cache:
                    try:
                        await websocket.send_text(cache)
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    cache = ''
                    last_sent_ts = time.time()

                if not logs:
                    try:
                        await asyncio.wait_for(websocket.receive(), timeout=0.2)
                        continue
                    except asyncio.TimeoutError:
                        continue
                    except (WebSocketDisconnect, RuntimeError):
                        break

                log = logs.popleft()

                if interval:
                    cache += f'{log}\n'
                    continue

                try:
                    await websocket.send_text(log)
                except (WebSocketDisconnect, RuntimeError):
                    break

        await websocket.close()


service = Service()
app.include_router(service.router)
