---
sidebar_position: 5
---

# Configuration

## Two numbers: `account` vs `operator`

signal-mcp separates the number it runs **as** from the number it talks **to**:

- **`account`** — the Signal number the `signal-cli daemon` is logged in as (its
  `-a`). Every message is sent *from* this number.
- **`operator`** — the human the agent serves. The `send` ("text me") tool messages
  this number, and in channel mode it's the default sender the agent listens to.
  This is who the agent talks *to*.

If you don't set `--account`, it defaults to `--operator`: the agent runs as you and
messages you — **Note to Self**, the right setup for a personal machine. Set them
to different values when the agent has its **own** number:

```bash
# Personal machine — account == operator == you (Note to Self)
signal-mcp --operator +15551234567

# Dedicated agent — sends FROM the agent number TO you
signal-mcp --account +353871760709 --operator +15551234567 --transport stdio
```

These are distinct from the [allowlists](#trusted-recipients--senders), which are
security *gates* — not addresses the agent sends to.

## Command-line arguments

Every argument has an environment-variable equivalent (`SIGNAL_MCP_` prefix); the
flag wins when both are set.

| Argument | Env var | Default | Description |
|----------|---------|---------|-------------|
| `--operator` *(required)* | `SIGNAL_MCP_OPERATOR` | — | E.164 number of the human the agent serves (who it messages / listens to). |
| `--account` | `SIGNAL_MCP_ACCOUNT` | *(= `--operator`)* | E.164 number the MCP runs **as** (the daemon's `-a`); messages are sent from it. |
| `--transport` | `SIGNAL_MCP_TRANSPORT` | `sse` | Transport: `sse` or `stdio` (use `stdio` for Claude Desktop/Code). |
| `--rpc-host` | `SIGNAL_MCP_RPC_HOST` | `127.0.0.1` | Host of the signal-cli daemon JSON-RPC interface. |
| `--rpc-port` | `SIGNAL_MCP_RPC_PORT` | `7583` | Port of the signal-cli daemon JSON-RPC interface. |
| `--channel` | `SIGNAL_MCP_CHANNEL` | `false` | Enable [Claude Channel mode](./channel-mode) (forces stdio). |
| `--prefix` | `SIGNAL_MCP_PREFIX` | *(none)* | Only forward messages starting with this prefix (channel mode); stripped before delivery. |
| `--trusted-recipient` | `SIGNAL_MCP_TRUSTED_RECIPIENTS` | *(none)* | Outbound allowlist — numbers/group ids the agent may message. Repeatable flag; comma-separated env var. Empty = all allowed. |
| `--trusted-sender` | `SIGNAL_MCP_TRUSTED_SENDERS` | *(none)* | Inbound allowlist — authors whose messages reach the agent (channel mode). Defaults to `--operator` when unset. |
| `--prompts-dir` | `SIGNAL_MCP_PROMPTS_DIR` | `~/.config/signal-mcp/prompts` | Directory of user-defined `*.md` prompt templates. |
| `--attachments-dir` | `SIGNAL_MCP_ATTACHMENTS_DIR` | `~/.local/share/signal-cli/attachments` | Where signal-cli stores received attachments. |
| `--attachment-transfer` | `SIGNAL_MCP_ATTACHMENT_TRANSFER` | `auto` | How outbound attachments reach the daemon: `path`, `data-uri`, or `auto`. |
| `--attachment-max-bytes` | `SIGNAL_MCP_ATTACHMENT_MAX_BYTES` | `26214400` | Largest local file encodable as a data URI (25 MB). |
| `--log-level` | `SIGNAL_MCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

S3-backed attachment storage adds a further `--s3-*` group; see the `--help`
output.

## Trusted recipients & senders

The two allowlists are **security gates**, not address books:

- **`--trusted-recipient`** restricts *outbound* sends. The agent still chooses
  each recipient (the `operator` for `send`, or an explicit `user_id`/`group_id`);
  any send to a target not on the allowlist is rejected before it reaches the
  daemon. Empty = every recipient allowed.
- **`--trusted-sender`** restricts *inbound* messages in channel mode: only
  listed authors are forwarded to the agent. When unset, only the `operator` is
  trusted (deny-by-default).

## Example configs

### Minimal (SSE, Note to Self)

```bash
signal-mcp --operator +15551234567
```

### Claude Code (stdio, dedicated agent number)

```json
{
  "mcpServers": {
    "signal": {
      "type": "stdio",
      "command": "signal-mcp",
      "args": [
        "--account",
        "+353871760709",
        "--operator",
        "+15551234567",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

### Channel mode + prefix

```json
{
  "mcpServers": {
    "signal": {
      "type": "stdio",
      "command": "signal-mcp",
      "args": [
        "--operator",
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
SIGNAL_MCP_RPC_HOST=10.0.0.5 SIGNAL_MCP_RPC_PORT=9090 \
  signal-mcp --operator +15551234567
```

## signal-cli daemon setup

The MCP connects to a running `signal-cli daemon` over TCP. Its `-a` is the
**account** — it must match the MCP's `--account`:

```bash
signal-cli -a ACCOUNT_NUMBER daemon --tcp 127.0.0.1:7583 \
  --receive-mode on-start --no-receive-stdout
```

| Flag | Purpose |
|------|---------|
| `-a` / `--account` | The registered phone number the daemon runs as (= MCP `--account`) |
| `--tcp HOST:PORT` | TCP endpoint for JSON-RPC |
| `--receive-mode on-start` | Always receiving (messages queued for clients) |
| `--no-receive-stdout` | Don't print received messages to stdout |

Run under a process supervisor (launchd, systemd, supervisord) for reliability.
