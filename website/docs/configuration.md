---
sidebar_position: 5
---

# Configuration

## Command-line arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--user-id` | *(required)* | Your Signal phone number (e.g. `+15551234567`) |
| `--transport` | `sse` | Transport protocol: `sse` or `stdio` |
| `--rpc-host` | `127.0.0.1` | Host of the signal-cli daemon JSON-RPC interface |
| `--rpc-port` | `7583` | Port of the signal-cli daemon JSON-RPC interface |
| `--channel` | `false` | Enable Claude Channel mode (forces stdio transport) |
| `--prefix` | *(none)* | Only forward messages starting with this prefix (channel mode) |

## Environment variables

All CLI arguments have corresponding environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `SIGNAL_CLI_RPC_HOST` | `127.0.0.1` | signal-cli daemon host |
| `SIGNAL_CLI_RPC_PORT` | `7583` | signal-cli daemon port |
| `SIGNAL_CHANNEL` | *(none)* | Set to `1`, `true`, or `yes` to enable channel mode |
| `SIGNAL_PREFIX` | *(none)* | Prefix filter for channel mode |

Environment variables are used as defaults — explicit CLI arguments take precedence.

## Example configs

### Minimal (SSE transport)

```bash
uv run signal_mcp/main.py --user-id +15551234567
```

### Claude Code (stdio transport)

```json
{
  "mcpServers": {
    "signal": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/signal-mcp",
        "python",
        "signal_mcp/main.py",
        "--user-id",
        "+15551234567",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

### Claude Code with channel mode + prefix

```json
{
  "mcpServers": {
    "signal": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/signal-mcp",
        "python",
        "signal_mcp/main.py",
        "--user-id",
        "+15551234567",
        "--channel",
        "--prefix",
        "cc"
      ]
    }
  }
}
```

### Custom daemon endpoint

```bash
SIGNAL_CLI_RPC_HOST=10.0.0.5 SIGNAL_CLI_RPC_PORT=9090 \
  uv run signal_mcp/main.py --user-id +15551234567
```

## signal-cli daemon setup

The MCP server connects to a running `signal-cli daemon` over TCP. The daemon should be started with:

```bash
signal-cli -a YOUR_PHONE_NUMBER daemon --tcp 127.0.0.1:7583 \
  --receive-mode on-start --no-receive-stdout
```

| Flag | Purpose |
|------|---------|
| `-a` / `--account` | The registered phone number |
| `--tcp HOST:PORT` | TCP endpoint for JSON-RPC |
| `--receive-mode on-start` | Always receiving (messages queued for clients) |
| `--no-receive-stdout` | Don't print received messages to stdout |

Run under a process supervisor (launchd, systemd, supervisord) for reliability.
