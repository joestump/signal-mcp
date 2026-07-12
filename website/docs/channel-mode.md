---
sidebar_position: 4
description: Push incoming Signal messages to Claude in real time without polling
---

# Claude Channel Mode

Channel mode is a powerful integration with [Claude Code](https://code.claude.com/) that pushes incoming Signal messages directly to Claude as they arrive — no polling required.

## How it works

Instead of Claude calling `receive_message` in a loop, the MCP server runs a background task that watches the signal-cli daemon's message queue. When a new message arrives, it's immediately forwarded to Claude via the `notifications/claude/channel` MCP notification.

```
Phone ──► Signal servers ──► signal-cli daemon ──► Signal MCP ──► Claude Code
                                                         │
                                          notifications/claude/channel
```

Claude sees the message as a `<channel>` tag in its conversation context and can respond immediately using the `send` or `send_message_to_user` tools.

## Enabling channel mode

Add the `--channel` flag (or set the `SIGNAL_CHANNEL` environment variable):

```bash
uv run signal_mcp/main.py --user-id YOUR_PHONE_NUMBER --channel
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
        "--user-id",
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

Send a **Note to Self** on Signal from your phone. Claude will see it arrive in real time.

## Prefix filtering

If you use Note to Self for things other than Claude, set a prefix so only tagged messages are forwarded. The prefix is stripped before delivery:

```bash
uv run signal_mcp/main.py --user-id YOUR_PHONE_NUMBER --channel --prefix cc
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

Claude has two ways to respond:

1. **`send`** — sends to the channel owner's phone (the `--user-id` number). Use when Claude proactively wants to notify you.
2. **`send_message_to_user`** — sends to any phone number. Use when replying to a specific sender from a channel message.

## Security considerations

- The channel server runs locally and communicates over stdio — no network exposure for the MCP protocol itself.
- Only forward messages from trusted senders. signal-cli as a linked device receives everything your phone receives.
- Use **prefix filtering** to limit what reaches Claude, reducing the risk of prompt injection from unexpected messages.
