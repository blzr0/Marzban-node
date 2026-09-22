import json as json_module
import socket

import click

from config import NODE_STATUS_SOCKET_PATH


def fetch_status(timeout: float = 5.0) -> dict:
    """Reads one JSON status blob from the local diagnostic socket (see
    status_socket.py). Works regardless of SERVICE_PROTOCOL (rest or rpyc)
    and regardless of whether the panel is reachable - it never touches the
    network, only the node's own filesystem.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(NODE_STATUS_SOCKET_PATH)
        except OSError as exc:
            raise RuntimeError(
                f"Could not connect to {NODE_STATUS_SOCKET_PATH}: {exc}. "
                "Is the node service (main.py) running?"
            ) from exc

        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)

    return json_module.loads(b"".join(chunks))


def format_uptime(seconds) -> str:
    seconds = int(seconds or 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def render_status(data: dict) -> str:
    lines = ["Marzban-node status", "─" * 34]

    if data.get("xray_running"):
        lines.append(
            f"Xray process:         running, pid {data.get('xray_pid')}, "
            f"uptime {format_uptime(data.get('xray_uptime_seconds'))}"
        )
    else:
        suffix = f" (last error: {data['last_error']})" if data.get("last_error") else ""
        lines.append(f"Xray process:         not running{suffix}")

    lines.append(f"Xray API:              {'reachable' if data.get('xray_api_reachable') else 'unreachable'}")

    if data.get("last_restart_reason"):
        lines.append(f"Last restart reason:   {data['last_restart_reason']}")

    lines.append("")

    sockets = data.get("listening_sockets") or []
    if sockets:
        lines.append("Active inbounds:")
        lines.append(f"  {'TAG':<12} {'PROTO':<6} {'PORT'}")
        for entry in sockets:
            lines.append(f"  {str(entry.get('tag')):<12} {str(entry.get('proto')):<6} {entry.get('port')}")
    else:
        lines.append("Active inbounds: none")

    return "\n".join(lines)


@click.group()
def cli():
    """Marzban-node command line interface."""


@cli.command()
@click.option("--json", "as_json", is_flag=True, default=False, help="Print raw JSON instead of a table.")
def status(as_json):
    """Show live Xray process/inbound status for this node."""
    try:
        data = fetch_status()
    except Exception as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1)

    if as_json:
        click.echo(json_module.dumps(data, indent=2))
    else:
        click.echo(render_status(data))


if __name__ == "__main__":
    cli()
