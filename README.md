# Signal MCP

> [!NOTE]
> A maintained fork of [rymurr/signal-mcp](https://github.com/rymurr/signal-mcp), whose last upstream commit was Apr 9, 2025 with none since — now maintained by [Joe Stump](https://github.com/joestump) and [Claude Code](https://claude.com/claude-code).

An [MCP](https://modelcontextprotocol.io) server for [signal-cli](https://github.com/AsamK/signal-cli)
that lets AI agents send and receive Signal messages — including emoji
reactions — through a long-running `signal-cli daemon`.

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Running the server](#running-the-server)
  - [Configuration](#configuration)
  - [Restricting recipients](#restricting-recipients-trusted-recipients)
- [Using with Claude](#using-with-claude-mcp-client-setup)
- [Tools](#tools)
- [Development](#development)

## Features

- Send messages to Signal users and groups
- React to messages with emoji (and remove reactions)
- Receive and parse incoming messages, including emoji reactions and
  "Note to Self" reaction syncs that plain-text parsing can't recover
- Restrict who the server may message with a trusted-recipients allowlist
- Talks to a persistent `signal-cli daemon`, so calls are fast and never fight
  over the account lock

## Architecture

This server is a thin **JSON-RPC client** for a long-running `signal-cli daemon`.
Instead of spawning a fresh `signal-cli` (and a fresh JVM) per request, it
connects to a persistent daemon over its newline-delimited JSON-RPC interface.
That daemon holds the Signal account lock for its lifetime, so:

- calls are instant — no ~2–3s JVM cold start each time, and
- concurrent callers (this MCP, scheduled jobs, manual use) no longer fight over
  the signal-cli account lock.

The daemon should run with `--receive-mode on-start`. Incoming messages arrive
as JSON-RPC `receive` notifications, which the server queues for
`receive_message`. signal-cli is typically a *linked* device, so the phone stays
the durable source of truth and a brief daemon outage loses nothing.

## Prerequisites

This project requires [signal-cli](https://github.com/AsamK/signal-cli) to be
installed and a Signal account registered with it.

1. **Install signal-cli** — follow the [official installation instructions](https://github.com/AsamK/signal-cli/blob/master/README.md#installation).

2. **Register your account:**
   ```bash
   signal-cli -a YOUR_PHONE_NUMBER register
   ```

3. **Verify** with the code received via SMS:
   ```bash
   signal-cli -a YOUR_PHONE_NUMBER verify CODE_RECEIVED
   ```

For more detail, see the [signal-cli documentation](https://github.com/AsamK/signal-cli/wiki).
Phone numbers are in [E.164](https://en.wikipedia.org/wiki/E.164) format
(e.g. `+15551234567`).

## Installation

```bash
# uv (recommended) — auto-syncs dependencies on first run
uv pip install -e .

# or plain pip
pip install -e .
```

The commands below use `uv run`, which syncs dependencies automatically, so a
separate install step is optional.

## Running the server

**1. Start the `signal-cli daemon`** (one warm process, JSON-RPC over TCP):

```bash
signal-cli -a YOUR_PHONE_NUMBER daemon \
  --tcp 127.0.0.1:7583 --receive-mode on-start --no-receive-stdout
```

Run the daemon under a supervisor (launchd / systemd) so it stays up.

**2. Start the MCP server**, which connects to that daemon:

```bash
uv run server --user-id YOUR_PHONE_NUMBER [--transport sse|stdio] \
  [--rpc-host 127.0.0.1] [--rpc-port 7583]
```

### Configuration

Every flag has an environment-variable equivalent; the flag wins when both are
set. All variables use the `SIGNAL_MCP_` prefix to avoid collisions.

| Flag | Env var | Default | Description |
| --- | --- | --- | --- |
| `--user-id` *(required)* | `SIGNAL_MCP_USER_ID` | — | Your Signal phone number (E.164). |
| `--transport` | `SIGNAL_MCP_TRANSPORT` | `sse` | MCP transport: `sse` or `stdio`. Use `stdio` for Claude Desktop/Code. |
| `--rpc-host` | `SIGNAL_MCP_RPC_HOST` | `127.0.0.1` | Host of the signal-cli daemon JSON-RPC interface. |
| `--rpc-port` | `SIGNAL_MCP_RPC_PORT` | `7583` | Port of the signal-cli daemon JSON-RPC interface. |
| `--trusted-recipient` | `SIGNAL_MCP_TRUSTED_RECIPIENTS` | *(none)* | Allowlist of recipients the server may message (comma-separated in the env var). See below. |

### Restricting recipients (trusted recipients)

By default the server can message any recipient. To enforce an allowlist so the
agent can only message recipients you have approved, pass `--trusted-recipient`
(repeatable) and/or set the comma-separated `SIGNAL_MCP_TRUSTED_RECIPIENTS`
environment variable. Values may be user phone numbers (E.164) or group
ids/names:

```bash
# Only allow messaging Alice and one group
uv run server --user-id YOUR_PHONE_NUMBER \
    --trusted-recipient +15555550101 \
    --trusted-recipient GROUP_ID

# Equivalent via environment variable
SIGNAL_MCP_TRUSTED_RECIPIENTS="+15555550101,GROUP_ID" \
    uv run server --user-id YOUR_PHONE_NUMBER
```

Both sources are merged. When the allowlist is non-empty, any `send_message_*`
or `send_reaction_*` call targeting a recipient that is not on it is rejected
with an error *before* the daemon is asked to send. When no trusted recipients
are configured, enforcement is disabled and every recipient is permitted.

## Using with Claude (MCP client setup)

Claude Desktop and Claude Code launch the server as a subprocess and talk to it
over **stdio**, so configure it with `--transport stdio`. The `signal-cli daemon`
must already be running (see [Running the server](#running-the-server)) — the
MCP server is just a client to it.

The launch command below uses [`uv`](https://docs.astral.sh/uv/) to run the
`server` entry point from a checkout. Replace `/ABSOLUTE/PATH/TO/signal-mcp`
with the path to this repository and `+15551234567` with your Signal number.

### Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config), which
lives at:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

Add a `signal` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "signal": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/ABSOLUTE/PATH/TO/signal-mcp",
        "server",
        "--user-id", "+15551234567",
        "--transport", "stdio"
      ],
      "env": {
        "SIGNAL_MCP_TRUSTED_RECIPIENTS": "+15555550101"
      }
    }
  }
}
```

Restart Claude Desktop for the change to take effect. The `env` block is
optional — use it to enforce the allowlist or point at a non-default daemon
(`SIGNAL_MCP_RPC_HOST` / `SIGNAL_MCP_RPC_PORT`). You can also pass
`--trusted-recipient` directly in `args`.

### Claude Code

Add the server with the `claude mcp add` CLI (everything after `--` is the
launch command):

```bash
claude mcp add signal \
  --env SIGNAL_MCP_TRUSTED_RECIPIENTS=+15555550101 \
  -- uv run --directory /ABSOLUTE/PATH/TO/signal-mcp \
     server --user-id +15551234567 --transport stdio
```

Use `--scope user` to make it available across all your projects (the default
is the current project). `--env KEY=value` (before the `--`) passes environment
variables such as `SIGNAL_MCP_TRUSTED_RECIPIENTS` or the `SIGNAL_MCP_RPC_*`
settings. This writes an `mcpServers` entry to your Claude Code config; you can
also hand-edit `.mcp.json` (project scope) with the same JSON shape shown above.
Verify with `claude mcp list`.

## Tools

The server exposes five tools. Every `send_*` tool returns
`{"message": "..."}` on success or `{"error": "..."}` on failure (including
when a recipient is blocked by the allowlist).

### `send_message_to_user(message, user_id)`

Send a direct message to a user.

- `message` *(str)* — the text to send.
- `user_id` *(str)* — recipient phone number (E.164).

### `send_message_to_group(message, group_id)`

Send a message to a group.

- `message` *(str)* — the text to send.
- `group_id` *(str)* — the group's internal id (the `group_name` returned by
  `receive_message`) **or** its display name; the server resolves either.

### `send_reaction_to_user(emoji, user_id, target_author, target_timestamp, remove=False)`

React to a user's message with an emoji.

- `emoji` *(str)* — the reaction emoji, e.g. `👍`.
- `user_id` *(str)* — recipient phone number (E.164).
- `target_author` *(str)* — author of the message being reacted to (use
  `sender_id` from `receive_message`).
- `target_timestamp` *(int)* — timestamp of that message (use `timestamp` from
  `receive_message`).
- `remove` *(bool, default `False`)* — set `True` to undo a previous reaction.

> To react to your own "Note to Self" message, set both `user_id` and
> `target_author` to your own number.

### `send_reaction_to_group(emoji, group_id, target_author, target_timestamp, remove=False)`

React to a message in a group. Same parameters as `send_reaction_to_user`,
except `group_id` (id or display name) identifies the group.

### `receive_message(timeout)`

Wait up to `timeout` seconds for the next actionable message (text body or
emoji reaction) and return it. Messages that arrived while the daemon was
streaming are queued, so back-to-back calls won't drop anything.

- `timeout` *(float)* — seconds to wait.

Returns a `MessageResponse` object. A **text message** populates `message`; an
**emoji reaction** populates `reaction` (with `message` left `null`), so callers
can tell the two apart:

```jsonc
{
  "message": "hey there",        // text body, or null for a reaction
  "sender_id": "+15551234567",   // who sent it
  "group_name": "GROUP_ID==",    // group id if it was a group message, else null
  "timestamp": 1744185565466,    // ms; pass back as target_timestamp to react
  "error": null,                 // set only on failure
  "reaction": null               // or a Reaction object (see below)
}
```

When the message is a reaction, `reaction` holds:

```jsonc
{
  "emoji": "👍",
  "target_author": "+15551234567",   // author of the message being reacted to
  "target_timestamp": 1744185565466, // timestamp of that message
  "is_remove": false                 // true when a reaction was removed
}
```

On timeout (or for non-actionable traffic like delivery/read receipts and
typing indicators) an empty `MessageResponse` is returned — all fields `null`,
which is **not** an error.

## Development

```bash
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy .                # type check
uv run --extra test pytest   # tests
```

Tests use a `FakeClient` stand-in for the daemon and JSON fixtures for the
parser, so no live `signal-cli` daemon is required.

## Acknowledgments

Forked from [rymurr/signal-mcp](https://github.com/rymurr/signal-mcp) by
[Ryan Murray](https://github.com/rymurr).

## License

Released under the [MIT License](LICENSE).
