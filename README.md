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
- **`cli.py status`**, run directly on the node (or via `docker exec`), or simply `marzban-node inbounds` if the node was installed with the script above:

  ```bash
  docker exec <container> python3 cli.py status
  # or add -j / --json for machine-readable output
  docker exec <container> python3 cli.py status --json
  ```

  This talks to a separate local-only Unix domain socket (`NODE_STATUS_SOCKET_PATH`, default `/var/run/marzban-node/status.sock`, mode `0600`) rather than the mTLS port - it works even if the panel is unreachable, since it never touches the network. Reaching it already requires the same filesystem/container access `docker exec` does, so no separate credential is needed.

## Panel reconnects don't restart Xray

A new panel connection no longer stops the running core: `/connect` only hands control over to the new session. The panel sends a fingerprint of the config it starts Xray with (users excluded) and the node reports it back - so after a panel restart or a network outage, a panel with the same config reattaches to the running core and syncs users over the API instead of restarting it, keeping client connections alive. The fingerprint is only reported to the IP the core was started for (the API routing rule in the running config admits only that IP); any other client gets a normal restart.

Needs [blzr0/Marzban](https://github.com/blzr0/Marzban) v0.8.36+. Older panels keep working with the previous restart-on-connect behavior.

## Xray comes back without the panel

The node saves the last config the panel started Xray with to `PERSISTED_CONFIG_FILE` (default `/var/lib/marzban-node/last_config.json`, mode `0600`) and uses it when the panel can't:

- **Autostart** - after a container restart or reboot, Xray starts from the saved config right away instead of waiting for the panel.
- **Watchdog** - if Xray dies on its own (panic, OOM kill) and the panel has been silent for 30s, the node restarts it. While the panel is reachable it's left to the panel, which restarts the core with fresh users. After 5 restarts within a minute the watchdog gives up until the panel starts Xray again. The exit code and last log line show up as `last_error` in `cli.py status`.

When the panel comes back it reattaches to this core (see above) and syncs the users changed meanwhile. Until then the users are those of the last panel start: users added since then can't connect, removed or expired ones still can. The file holds user UUIDs and Reality private keys, and is deleted when the panel stops the core explicitly (`/stop`). Set `PERSIST_CONFIG=false` to turn all of this off. REST only.
