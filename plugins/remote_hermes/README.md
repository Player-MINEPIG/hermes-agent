# remote_hermes

`remote_hermes` lets a coordinator Hermes route tasks to local Hermes API
servers on other devices. It is intended for a lightweight VPS or relay node,
not for every local execution device.

## Tools

- `remote_hermes_list`: list configured nodes without revealing API keys.
- `remote_hermes_health`: check a node's `/health` endpoint.
- `remote_hermes_run`: send a task to a node's `/v1/responses` endpoint.

## Configuration

Create a coordinator-only env file:

```bash
cp plugins/remote_hermes/remote-hermes-nodes.env.example ~/.hermes/remote-hermes-nodes.env
chmod 600 ~/.hermes/remote-hermes-nodes.env
```

Then replace the placeholder API bases and keys.

The file path can be overridden with:

```bash
REMOTE_HERMES_NODES_FILE=/path/to/remote-hermes-nodes.env
```

## Security

- Keep `remote-hermes-nodes.env` out of git.
- Bind each local Hermes API server to its Tailscale IP, not `0.0.0.0`.
- Use Tailscale ACLs so only the coordinator can reach local Hermes API ports.
- Do not enable this plugin on devices that do not need to route work.
