---
sidebar_position: 2
---

# Installation

## Prerequisites

1. **Python 3.13+** — [python.org](https://www.python.org/) or `brew install python`
2. **[signal-cli](https://github.com/AsamK/signal-cli)** — a command-line interface for Signal
3. **[uv](https://docs.astral.sh/uv/)** (recommended) or `pip` for Python package management

### Installing signal-cli

Follow the [official installation instructions](https://github.com/AsamK/signal-cli/blob/master/README.md#installation).

**Register your Signal account** (if using a dedicated number):

```bash
signal-cli -u YOUR_PHONE_NUMBER register
signal-cli -u YOUR_PHONE_NUMBER verify CODE_RECEIVED
```

**Link as a secondary device** (recommended — keeps your phone as primary):

```bash
signal-cli link --qr-url-output qrcode.png
# Scan qrcode.png with Signal > Settings > Linked Devices > Link New Device
```

signal-cli will print a phone number to use for `--account` going forward.

## Install Signal MCP

```bash
git clone https://github.com/joestump/signal-mcp.git
cd signal-mcp
uv pip install -e .
```

Or with pip:

```bash
pip install -e .
```

## Start the daemon

signal-cli must run as a persistent daemon so the MCP server can connect to it:

```bash
signal-cli -a YOUR_PHONE_NUMBER daemon --tcp 127.0.0.1:7583 \
  --receive-mode on-start --no-receive-stdout
```

:::tip
Run the daemon under a supervisor (launchd on macOS, systemd on Linux) so it stays up and restarts on crash.
:::

## Run the MCP server

### With Claude Code

Add to your `.mcp.json` or `~/.claude.json`:

```json
{
  "mcpServers": {
    "signal": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/signal-mcp", "python", "signal_mcp/main.py", "--operator", "+15551234567", "--transport", "stdio"]
    }
  }
}
```

### Standalone

```bash
uv run signal_mcp/main.py --operator YOUR_PHONE_NUMBER [--transport {sse|stdio}] \
  [--rpc-host 127.0.0.1] [--rpc-port 7583]
```

The daemon endpoint defaults to `127.0.0.1:7583` and can also be set via `SIGNAL_CLI_RPC_HOST` / `SIGNAL_CLI_RPC_PORT` environment variables.
