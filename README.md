# Signal MCP

> [!NOTE]
> A maintained fork of [rymurr/signal-mcp](https://github.com/rymurr/signal-mcp) (abandoned) and [BrendanMartin/claude-channel-signal](https://github.com/BrendanMartin/claude-channel-signal) (abandoned) — now maintained by [Joe Stump](https://github.com/joestump) and [Claude Code](https://claude.com/claude-code).

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
- [Claude Channel mode](#claude-channel-mode)
  - [How it works](#how-channel-mode-works)
  - [Channel mode configuration](#channel-mode-configuration)
  - [Prefix filtering](#prefix-filtering)
  - [Claude Code channel setup](#claude-code-channel-setup)
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

For more detail, see the [Signal MCP documentation](https://joestump.github.io/signal-mcp/).
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
uv run signal-mcp --user-id YOUR_PHONE_NUMBER [--transport sse|stdio] \
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
| `--log-level` | `SIGNAL_MCP_LOG_LEVEL` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

### Restricting recipients (trusted recipients)

By default the server can message any recipient. To enforce an allowlist so the
agent can only message recipients you have approved, pass `--trusted-recipient`
(repeatable) and/or set the comma-separated `SIGNAL_MCP_TRUSTED_RECIPIENTS`
environment variable. Values may be user phone numbers (E.164) or group
ids/names:

```bash
# Only allow messaging Alice and one group
uv run signal-mcp --user-id YOUR_PHONE_NUMBER \
    --trusted-recipient +15555550101 \
    --trusted-recipient GROUP_ID

# Equivalent via environment variable
SIGNAL_MCP_TRUSTED_RECIPIENTS="+15555550101,GROUP_ID" \
    uv run signal-mcp --user-id YOUR_PHONE_NUMBER
```

Both sources are merged. When the allowlist is non-empty, any `send_message_*`
or `send_reaction_*` call targeting a recipient that is not on it is rejected
with an error *before* the daemon is asked to send. When no trusted recipients
are configured, enforcement is disabled and every recipient is permitted.

For groups, the allowlist entry may be either the group's internal id or its
display name — the server resolves the group and accepts the send if *either*
form is allowlisted, regardless of which form the caller passed.

## Using with Claude (MCP client setup)

Claude Desktop and Claude Code launch the server as a subprocess and talk to it
over **stdio**, so configure it with `--transport stdio`. The `signal-cli daemon`
must already be running (see [Running the server](#running-the-server)) — the
MCP server is just a client to it.

The launch command below uses [`uv`](https://docs.astral.sh/uv/) to run the
`signal-mcp` entry point from a checkout. Replace `/ABSOLUTE/PATH/TO/signal-mcp`
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
        "signal-mcp",
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
     signal-mcp --user-id +15551234567 --transport stdio
```

Use `--scope user` to make it available across all your projects (the default
is the current project). `--env KEY=value` (before the `--`) passes environment
variables such as `SIGNAL_MCP_TRUSTED_RECIPIENTS` or the `SIGNAL_MCP_RPC_*`
settings. This writes an `mcpServers` entry to your Claude Code config; you can
also hand-edit `.mcp.json` (project scope) with the same JSON shape shown above.
Verify with `claude mcp list`.

## Claude Channel mode

Claude Code supports a **channel** transport that pushes incoming messages to
Claude in real time instead of requiring Claude to poll `receive_message`. In
this mode the server runs a background forwarder that listens on the signal-cli
daemon and emits `notifications/claude/channel` JSON-RPC notifications to
Claude as messages arrive.

Channel mode is useful when you want Claude to react immediately to incoming
Signal messages without a polling loop — Claude "sees" every message the
moment it arrives.

### How channel mode works

- Transport is forced to **stdio** (Claude launches the server as a
  subprocess, same as normal stdio mode).
- The server declares the `claude/channel` experimental capability so Claude
  knows to expect push notifications.
- A background task listens on the signal-cli daemon and forwards each text
  message as a `notifications/claude/channel` notification. Reactions are not
  forwarded.
- Claude receives messages wrapped as `<channel source="signal"
  sender="..." sender_name="..." group="...">`, and can reply with the
  `send` tool (no recipient needed — it always messages the channel owner's
  phone).

### Channel mode configuration

| Flag | Env var | Default | Description |
| --- | --- | --- | --- |
| `--channel` | `SIGNAL_MCP_CHANNEL` | `false` | Enable Claude Channel mode. |
| `--prefix` | `SIGNAL_MCP_PREFIX` | *(none)* | Only forward messages starting with this prefix (case-insensitive). The prefix is stripped before delivery. |

All other flags (`--user-id`, `--rpc-host`, `--rpc-port`,
`--trusted-recipient`) work the same as in normal mode.

### Prefix filtering

When `--prefix` is set, only messages whose body starts with the prefix (after
leading whitespace, case-insensitive) are forwarded to Claude. The prefix must
end on a word boundary — prefix `cc` matches `cc deploy` but not `ccdeploy`.
The prefix is stripped from the message before delivery. Messages that don't
match are dropped silently.

This is useful when the signal-cli daemon receives messages from multiple
sources and you only want Claude to see a subset — for example, only messages
prefixed with `claude`:

```bash
uv run signal-mcp --user-id YOUR_PHONE_NUMBER --channel --prefix "claude"
```

### Claude Code channel setup

Add the server with the `--channel` flag:

```bash
claude mcp add signal \
  --scope user \
  --env SIGNAL_MCP_TRUSTED_RECIPIENTS=+15555550101 \
  -- uv run --directory /ABSOLUTE/PATH/TO/signal-mcp \
     signal-mcp --user-id +15551234567 --channel
```

The `--prefix` flag is optional — add it if you want selective forwarding:

```bash
claude mcp add signal \
  -- uv run --directory /ABSOLUTE/PATH/TO/signal-mcp \
     signal-mcp --user-id +15551234567 --channel --prefix "claude"
```

## Tools

The server exposes **seven** tools. In channel mode, messages are automatically
marked as read when forwarded. Every `send_*` and `mark_read` tool returns
`{"message": "..."}` on success; failures (including recipients blocked by the
allowlist) are reported as proper MCP tool errors (`isError`) with the reason
in the error message, not as an `{"error": ...}` payload inside a successful
result.

### `send(message, attachments=None)`

Send a message to the channel owner's phone. No recipient is needed — it
always messages the `--user-id` account.

- `message` *(str)* — the text to send. May be empty when `attachments` are
  provided.
- `attachments` *(list[str], optional)* — attachments to send (see
  [Attachments](#attachments)).

### `send_message_to_user(message, user_id, attachments=None)`

Send a direct message to a user.

- `message` *(str)* — the text to send. May be empty when `attachments` are
  provided.
- `user_id` *(str)* — recipient phone number (E.164).
- `attachments` *(list[str], optional)* — attachments to send (see
  [Attachments](#attachments)).

### `send_message_to_group(message, group_id, attachments=None)`

Send a message to a group.

- `message` *(str)* — the text to send. May be empty when `attachments` are
  provided.
- `group_id` *(str)* — the group's internal id (the `group_id` returned by
  `receive_message`) **or** its display name; the server resolves either.
- `attachments` *(list[str], optional)* — attachments to send (see
  [Attachments](#attachments)).

### Attachments

Each entry in an `attachments` list is either:

- a **file path** — `~` is expanded and the path is resolved to an absolute
  path. The file must exist and be readable, or the tool errors out before
  anything is sent; or
- an **RFC 2397 data URI** — `data:<MIME>;filename=<NAME>;base64,<DATA>`,
  passed through to signal-cli unchanged.

> **Remote daemon caveat:** file paths are resolved on the host where the
> signal-cli daemon runs. When the daemon is remote (a different machine or a
> container without a shared filesystem), local paths won't exist there — use
> data URIs instead, which embed the file content and work regardless of where
> the daemon lives.

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

### `receive_message(timeout=60)`

Wait up to `timeout` seconds for the next actionable message (text body or
emoji reaction) and return it. Messages that arrived while the daemon was
streaming are queued, so back-to-back calls won't drop anything.

- `timeout` *(float, default `60`)* — seconds to wait.

Returns a `MessageResponse` object. A **text message** populates `message`; an
**emoji reaction** populates `reaction` (with `message` left `null`), so callers
can tell the two apart:

```jsonc
{
  "message": "hey there",         // text body, or null for a reaction
  "sender_id": "+15551234567",    // who sent it
  "sender_name": "Alice Example", // sender's profile/contact name, if known
  "group_id": "GROUP_ID==",       // group id if it was a group message, else null
  "timestamp": 1744185565466,     // ms; pass back as target_timestamp to react
  "reaction": null                // or a Reaction object (see below)
}
```

> **Breaking change:** this field was previously named `group_name` (it always
> carried the group *id*), and failures were previously reported via an `error`
> field. Failures are now raised as MCP tool errors instead.

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

### `mark_read(sender, target_timestamp)`

Mark a received message as read in Signal. Useful in normal (polling) mode
after `receive_message` returns. In channel mode this is done automatically.

- `sender` *(str)* — the sender's phone number (use `sender_id` from the
  received message).
- `target_timestamp` *(int)* — the message timestamp (use `timestamp` from
  the received message).

Returns `{"message": "Read receipt sent"}` on success.

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
