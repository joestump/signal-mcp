---
sidebar_position: 5
---

# A2UI chat surfaces

When the MCP host supports [A2UI](https://a2ui.org), signal-mcp serves
read-only chat-surface resources that render Signal conversations as native
UI — two-sided message bubbles, sender names, human-readable timestamps,
attachment lines, and emoji reactions on their target bubbles.

## Resources

Four resource URIs are registered (two schemes × two surfaces):

| URI | What it renders |
|-----|-----------------|
| `signal://conversations/a2ui{?w}` | Index of buffered conversations |
| `mcp://signal/conversations/a2ui{?w}` | (same) |
| `signal://conversation/{id}/a2ui{?w}` | One conversation thread |
| `mcp://signal/conversation/{id}/a2ui{?w}` | (same) |

`{id}` is a phone number (E.164) or a Signal group id. Group ids are
base64 and may contain `/` and `=`, so they **must be percent-encoded** in
the URI.

`{?w}` is the optional width hint A2UI hosts append uniformly to any `/a2ui`
URI (`?w=112`). Both surfaces accept it and currently ignore it; the bare URI
works identically. Declaring it makes every URI a template, so all four
advertise under `resourceTemplates/list` rather than `resources/list`.

All resources declare MIME `application/a2ui+json` and `audience: ["user"]`.
The model's programmatic tools (`receive_message`, sends, reactions,
`mark_read`) are unchanged — resources are purely additive.

## Scope and limitations

History is **in-memory, instance-local, and cleared on restart**. The
buffer covers only traffic this server process observed during its own
lifetime, plus its own outbound sends. The phone is the only complete
record — this is not a message archive. Two concurrently running instances
will legitimately show different things.

## Configuration

Three configurable caps bound memory use:

| Flag | Env var | Default | Semantics |
|------|---------|---------|-----------|
| `--history-message-cap` | `SIGNAL_MCP_HISTORY_MESSAGE_CAP` | 200 | Messages per conversation (FIFO eviction) |
| `--history-conversation-cap` | `SIGNAL_MCP_HISTORY_CONVERSATION_CAP` | 50 | Total conversations (LRU eviction) |
| `--history-text-cap` | `SIGNAL_MCP_HISTORY_TEXT_CAP` | 4096 | Stored text bytes per message (truncation with marker) |

## Reference

- [A2UI-over-MCP transport guide](https://a2ui.org/guides/a2ui_over_mcp/) (normative)
- [ADR-0001](https://github.com/joestump/signal-mcp/blob/main/docs/adrs/ADR-0001-a2ui-chat-surfaces-over-mcp-resources.md)
- [SPEC-0001](https://github.com/joestump/signal-mcp/blob/main/docs/openspec/specs/a2ui-chat-surfaces/spec.md)
