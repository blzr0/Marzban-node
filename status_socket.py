import atexit
import json
import os
import socket
import threading

from logger import logger


def serve_status_over_unix_socket(socket_path: str, get_status: callable) -> "socket.socket | None":
    """Starts a background thread accepting connections on a Unix domain
    socket; each connection gets one JSON status blob, then is closed.

    This is deliberately separate from the mTLS-protected SERVICE_PORT: it
    exists purely so cli.py can read live status while running locally on
    the node (docker exec, or directly on the host), including when the
    panel is unreachable. There's no session/handshake/auth of its own -
    the socket file itself (mode 0600, only readable/connectable by whoever
    can already reach the filesystem it lives on) is the access boundary,
    which is the same boundary docker exec already requires. Never expose
    this path over the network.
    """
    try:
        directory = os.path.dirname(socket_path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as exc:
        logger.warning(f"Could not create directory for status socket {socket_path}: {exc}")
        return None

    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # bind() creates the socket file with whatever the process umask allows,
    # which is 022 (world-connectable) more often than not - narrow that to
    # owner-only *before* the file exists, rather than chmod()'ing after
    # (which would leave a brief window where anyone can connect).
    old_umask = os.umask(0o177)
    try:
        server.bind(socket_path)
    except OSError as exc:
        logger.warning(f"Could not bind status socket at {socket_path}: {exc}")
        return None
    finally:
        os.umask(old_umask)

    os.chmod(socket_path, 0o600)  # belt-and-suspenders, in case of a odd umask/filesystem
    server.listen(5)

    def cleanup():
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass

    atexit.register(cleanup)

    def accept_loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                break

            try:
                payload = json.dumps(get_status()).encode()
                conn.sendall(payload)
            except Exception as exc:
                logger.debug(f"status socket request failed: {exc}")
            finally:
                conn.close()

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    logger.info(f"Diagnostic status socket listening at {socket_path}")

    return server
