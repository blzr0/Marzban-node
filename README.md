# Marzban-node

> Forked from [Gozargah/Marzban-node](https://github.com/Gozargah/Marzban-node) with a patch that keeps Xray running when the master panel goes offline.

## Quick install

```bash
sudo bash -c "$(curl -sL https://github.com/blzr0/Marzban-scripts/raw/master/marzban-node.sh)" @ install
```

Install with a custom name:

```bash
sudo bash -c "$(curl -sL https://github.com/blzr0/Marzban-scripts/raw/master/marzban-node.sh)" @ install --name marzban-node2
```

Install only the management script:

```bash
sudo bash -c "$(curl -sL https://github.com/blzr0/Marzban-scripts/raw/master/marzban-node.sh)" @ install-script
```

Use `help` to view all commands:

```bash
marzban-node help
```

## Diagnostics

Two independent ways to check whether Xray is actually running, without shelling in and grepping logs:

- **`GET /status`** on the management API (REST or RPyC's `status()`, depending on `SERVICE_PROTOCOL`), reachable over the same mTLS-protected `SERVICE_PORT` as the rest of the panel-node channel.
- **`cli.py status`**, run directly on the node (or via `docker exec`):

  ```bash
  docker exec <container> python3 cli.py status
  # or add --json for machine-readable output
  docker exec <container> python3 cli.py status --json
  ```

  This talks to a separate local-only Unix domain socket (`NODE_STATUS_SOCKET_PATH`, default `/var/run/marzban-node/status.sock`, mode `0600`) rather than the mTLS port - it works even if the panel is unreachable, since it never touches the network. Reaching it already requires the same filesystem/container access `docker exec` does, so no separate credential is needed.
