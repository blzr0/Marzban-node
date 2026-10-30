from decouple import config
from dotenv import load_dotenv

load_dotenv()

SERVICE_HOST = config("SERVICE_HOST", default="0.0.0.0")
SERVICE_PORT = config('SERVICE_PORT', cast=int, default=62050)

XRAY_API_HOST = config("XRAY_API_HOST", default="0.0.0.0")
XRAY_API_PORT = config('XRAY_API_PORT', cast=int, default=62051)
XRAY_EXECUTABLE_PATH = config("XRAY_EXECUTABLE_PATH", default="/usr/local/bin/xray")
XRAY_ASSETS_PATH = config("XRAY_ASSETS_PATH", default="/usr/local/share/xray")

SSL_CERT_FILE = config("SSL_CERT_FILE", default="/var/lib/marzban-node/ssl_cert.pem")
SSL_KEY_FILE = config("SSL_KEY_FILE", default="/var/lib/marzban-node/ssl_key.pem")
SSL_CLIENT_CERT_FILE = config("SSL_CLIENT_CERT_FILE", default="")

DEBUG = config("DEBUG", cast=bool, default=False)

SERVICE_PROTOCOL = config('SERVICE_PROTOCOL', cast=str, default='rest')

INBOUNDS = config("INBOUNDS", cast=lambda v: [x.strip() for x in v.split(',')] if v else [], default="")

# Local-only diagnostic status socket (used by cli.py). Unix domain socket,
# not part of the mTLS-protected SERVICE_PORT: reading it already requires
# the same filesystem access as docker exec-ing into the node, so no extra
# auth is layered on top of it.
NODE_STATUS_SOCKET_PATH = config("NODE_STATUS_SOCKET_PATH", default="/var/run/marzban-node/status.sock")

# Last config the panel started Xray with, kept on disk so the node brings
# Xray back up on its own (container restart, reboot, Xray crash) while the
# panel is unreachable. Holds user UUIDs and Reality keys - stored 0600 next
# to the node's SSL key.
PERSIST_CONFIG = config("PERSIST_CONFIG", cast=bool, default=True)
PERSISTED_CONFIG_FILE = config("PERSISTED_CONFIG_FILE", default="/var/lib/marzban-node/last_config.json")
