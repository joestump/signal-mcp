# Signal MCP

An [MCP](https://github.com/mcp-signal/mcp) integration for [signal-cli](https://github.com/AsamK/signal-cli) that allows AI agents to send and receive Signal messages.

## Features

- Send messages to Signal users
- Send messages to Signal groups
- Receive and parse incoming messages
- Async support with timeout handling
- Detailed logging

## Architecture

This server is a thin **JSON-RPC client** for a long-running `signal-cli daemon`.
Instead of spawning a fresh `signal-cli` (and a fresh JVM) per request, it
connects to a persistent daemon over its newline-delimited JSON-RPC interface.
That daemon holds the Signal account lock for its lifetime, so:

- calls are instant — no ~2-3s JVM cold start each time, and
- concurrent callers (this MCP, scheduled jobs, manual use) no longer fight over
  the signal-cli account lock.

The daemon should run with `--receive-mode on-start`. Incoming messages arrive as
JSON-RPC `receive` notifications, which the server queues for `receive_message`.
signal-cli is typically a *linked* device, so the phone stays the durable source
of truth and a brief daemon outage loses nothing.

## Prerequisites

This project requires [signal-cli](https://github.com/AsamK/signal-cli) to be installed and configured on your system.

### Installing signal-cli

1. **Install signal-cli**: Follow the [official installation instructions](https://github.com/AsamK/signal-cli/blob/master/README.md#installation)

2. **Register your Signal account**:
   ```bash
   signal-cli -u YOUR_PHONE_NUMBER register
   ```

3. **Verify your account** with the code received via SMS:
   ```bash
   signal-cli -u YOUR_PHONE_NUMBER verify CODE_RECEIVED
   ```

For more detailed setup instructions, see the [signal-cli documentation](https://github.com/AsamK/signal-cli/wiki).

## Installation

```bash
pip install -e .
# or use uv for faster installation
uv pip install -e .
```

## Usage

First, run the signal-cli daemon (one warm process, JSON-RPC over TCP):

```bash
signal-cli -a YOUR_PHONE_NUMBER daemon --tcp 127.0.0.1:7583 --receive-mode on-start --no-receive-stdout
```

Then run the MCP server, which connects to that daemon:

```bash
./main.py --user-id YOUR_PHONE_NUMBER [--transport {sse|stdio}] \
          [--rpc-host 127.0.0.1] [--rpc-port 7583]
```

The daemon endpoint defaults to `127.0.0.1:7583` and can also be set via the
`SIGNAL_CLI_RPC_HOST` / `SIGNAL_CLI_RPC_PORT` environment variables. Run the
daemon under a supervisor (launchd / systemd) so it stays up.

## API

### Tools Available

- `send_message_to_user`: Send a direct message to a Signal user
- `send_message_to_group`: Send a message to a Signal group
- `send_reaction_to_user`: React to a user's message with an emoji (set `remove=True` to undo)
- `send_reaction_to_group`: React to a message in a group with an emoji
- `receive_message`: Wait for and receive messages with timeout support. Returns either a text `message` or a structured `reaction` (emoji + target), so emoji reactions — including "Note to Self" reactions — come through instead of erroring

## Development

This project uses:
- [MCP](https://github.com/mcp-signal/mcp) for agent-API integration
- Modern Python async patterns
- Type annotations throughout
