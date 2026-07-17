---
sidebar_position: 4
description: Push incoming Signal messages to Claude in real time without polling
---

# Claude Channel Mode

Channel mode is a powerful integration with [Claude Code](https://code.claude.com/) and [Crush](https://github.com/charmbracelet/crush) that pushes incoming Signal messages directly to your agent as they arrive — no polling required.

## How it works

Instead of Claude calling `receive_message` in a loop, the MCP server runs a background task that watches the signal-cli daemon's message queue. When a new message arrives, it's immediately forwarded to Claude via the `notifications/claude/channel` MCP notification.

```
Phone ──► Signal servers ──► signal-cli daemon ──► Signal MCP ──► Agent (Claude/Crush)
                                                         │
                                          notifications/claude/channel
                                                         │
                                              sendReceipt (auto mark-read
                                              to message author)
```

Claude sees the message as a `<channel>` tag in its conversation context (Crush does the same) and can respond immediately using the `send` or `send_message_to_user` tools.

## Enabling channel mode

Add the `--channel` flag (or set the `SIGNAL_CHANNEL` environment variable):

```bash
uv run signal_mcp/main.py --operator YOUR_PHONE_NUMBER --channel
```

Or in your MCP config (`~/.claude.json` or `.mcp.json`):

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
        "--operator",
        "+15551234567",
        "--channel"
      ],
      "env": {
        "SIGNAL_MCP_CHANNEL": "1"
      }
    }
  }
}
```

Then start Claude Code with the channel loaded:

```bash
claude --dangerously-load-development-channels server:signal
```

Send a **Note to Self** on Signal from your phone. Your agent will see it arrive in real time.

## Trusted senders (inbound gating)

Channel mode pushes message text straight into your agent's context, so inbound gating is **deny-by-default**: with no trusted senders configured, only messages whose envelope `source` equals `--operator` — your own number, e.g. Note to Self — are forwarded. Anything else is dropped with a log line: no notification, no read receipt.

To let other people through, configure the allowlist with the repeatable `--trusted-sender` flag or the comma-separated `SIGNAL_MCP_TRUSTED_SENDERS` environment variable:

```bash
uv run signal_mcp/main.py --operator +15551234567 --channel \
    --trusted-sender +15551234567 \
    --trusted-sender +15555550101
```

Or in your MCP config:

```json
{
  "env": {
    "SIGNAL_MCP_TRUSTED_SENDERS": "+15551234567,+15555550101"
  }
}
```

Once configured, the list is exhaustive — include your own number if you still want Note to Self forwarded.

The check always applies to the message **author** (the envelope `source`), never the group id: membership in a group — even an allowlisted one — cannot be used to inject prompts.

In normal (polling) mode the same filter applies to `receive_message`, but only when trusted senders are configured — unconfigured polling is unchanged.

## Prefix filtering

If you use Note to Self for things other than Claude, set a prefix so only tagged messages are forwarded. The prefix is stripped before delivery:

```bash
uv run signal_mcp/main.py --operator YOUR_PHONE_NUMBER --channel --prefix cc
```

Or via environment variable:

```json
{
  "env": {
    "SIGNAL_MCP_PREFIX": "cc"
  }
}
```

With the prefix set to `cc`:

- **"cc what's the weather"** → forwarded as "what's the weather"
- **"buy milk"** → silently ignored

Matching is case-insensitive.

## Message format

Incoming messages arrive in Claude's context as `<channel>` tags:

```xml
<channel source="signal" sender="+15551234567" group="group-id-here==">
  Message body text here
</channel>
```

| Attribute | Description |
|-----------|-------------|
| `source` | Always `signal` |
| `sender` | The Signal phone number of the sender |
| `group` | Present only for group messages (the group's internal ID) |

## Reply tools

Agents have two ways to respond:

1. **`send`** — sends to the channel operator's phone (the `--operator` number). Use when the agent proactively wants to notify you.
2. **`send_message_to_user`** — sends to any phone number. Use when replying to a specific sender from a channel message.

## Security considerations

- The channel server runs locally and communicates over stdio — no network exposure for the MCP protocol itself.
- signal-cli as a linked device receives everything your phone receives, so inbound messages are gated on the **trusted senders** allowlist (see above). In channel mode this is deny-by-default: with nothing configured, only your own messages (`--operator`) reach the agent.
- Use **prefix filtering** on top of sender gating to limit *which* of the trusted messages reach Claude — the prefix filters content, the allowlist filters identity.
