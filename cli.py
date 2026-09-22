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
    lines = [click.style("Marzban-node · live status", bold=True), "═" * 38, ""]

    if data.get("xray_running"):
        dot = click.style("●", fg="green")
        state = click.style("running", fg="green", bold=True)
        lines.append(
            f"  Xray process    {dot}  {state}   "
            f"pid {data.get('xray_pid')}, uptime {format_uptime(data.get('xray_uptime_seconds'))}"
        )
    else:
        dot = click.style("●", fg="red")
        state = click.style("not running", fg="red", bold=True)
        suffix = f"  (last error: {data['last_error']})" if data.get("last_error") else ""
        lines.append(f"  Xray process    {dot}  {state}{suffix}")

    api_ok = data.get("xray_api_reachable")
    dot = click.style("●", fg="green" if api_ok else "red")
    state = click.style(
        "reachable" if api_ok else "unreachable",
        fg="green" if api_ok else "red",
        bold=True,
    )
    lines.append(f"  Xray API        {dot}  {state}")

    if data.get("last_restart_reason"):
        lines.append(f"  Last restart    {data['last_restart_reason']}")

    lines.append("")

    sockets = data.get("listening_sockets") or []
    if sockets:
        # Fixed-width padding misaligns PORT the moment a real tag is
        # longer than the assumed width, so size TAG to the actual data
        # (with a floor at the header's own width) instead of guessing.
        tag_width = max([len("TAG")] + [len(str(entry.get("tag"))) for entry in sockets])

        lines.append(click.style("  Active inbounds", bold=True))
        lines.append("  " + "─" * 36)
        lines.append(f"  {'TAG':<{tag_width}} {'PROTO':<7} {'PORT'}")
        for entry in sockets:
            proto = str(entry.get("proto"))
            proto_colored = click.style(f"{proto:<7}", fg="cyan")
            lines.append(f"  {str(entry.get('tag')):<{tag_width}} {proto_colored} {entry.get('port')}")
    else:
        lines.append(click.style("  Active inbounds: none", fg="yellow"))

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
