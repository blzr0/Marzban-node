import asyncio
import json
import time
from typing import Optional
from uuid import UUID, uuid4

from fastapi import (APIRouter, Body, FastAPI, HTTPException, Request,
                     WebSocket, status)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from config import (NODE_STATUS_SOCKET_PATH, SERVICE_PROTOCOL,
                    XRAY_ASSETS_PATH, XRAY_EXECUTABLE_PATH)
from logger import logger
from status_socket import serve_status_over_unix_socket
from xray import XRayConfig, XRayCore

app = FastAPI()


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

        if SERVICE_PROTOCOL == "rest":
            serve_status_over_unix_socket(NODE_STATUS_SOCKET_PATH, self.status)

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

    def start(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True),
              config_fingerprint: Optional[str] = Body(None, embed=True)):
        self.match_session_id(session_id)
        self.config_fingerprint = None

        try:
            config = XRayConfig(config, self.client_ip)
        except json.decoder.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "config": f'Failed to decode config: {exc}'
                }
            )

        with self.core.get_logs() as logs:
            try:
                self.core.start(config)

                start_time = time.time()
                end_time = start_time + 3
                last_log = ''
                while time.time() < end_time:
                    while logs:
                        log = logs.popleft()
                        if log:
                            last_log = log
                        if f'Xray {self.core_version} started' in log:
                            break
                    time.sleep(0.1)

            except Exception as exc:
                logger.error(f"Failed to start core: {exc}")
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
        return self.response()

    def stop(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)

        try:
            self.core.stop()

        except RuntimeError:
            pass

        self.config_fingerprint = None
        return self.response()

    def restart(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True),
              config_fingerprint: Optional[str] = Body(None, embed=True)):
        self.match_session_id(session_id)
        self.config_fingerprint = None

        try:
            config = XRayConfig(config, self.client_ip)
        except json.decoder.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "config": f'Failed to decode config: {exc}'
                }
            )

        try:
            with self.core.get_logs() as logs:
                self.core.restart(config, reason="panel_requested")

                start_time = time.time()
                end_time = start_time + 3
                last_log = ''
                while time.time() < end_time:
                    while logs:
                        log = logs.popleft()
                        if log:
                            last_log = log
                        if f'Xray {self.core_version} started' in log:
                            break
                    time.sleep(0.1)

        except Exception as exc:
            logger.error(f"Failed to restart core: {exc}")
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
        return self.response()

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
