---
sidebar_position: 3
---

# Tools

Signal MCP exposes the following MCP tools:

## `send`

Send a message to the channel operator's phone. No phone number needed — this uses the `--operator` configured at startup.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message` | `str` | Yes | The message to send |

```python
send(message="Hey, the deploy is done!")
```

## `send_message_to_user`

Send a direct message to a specific Signal user.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message` | `str` | Yes | The message to send |
| `user_id` | `str` | Yes | Recipient phone number (e.g. `+15551234567`) |

```python
send_message_to_user(message="Hello!", user_id="+15551234567")
```

## `send_message_to_group`

Send a message to a Signal group. The `group_id` can be the group's internal ID or its display name.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message` | `str` | Yes | The message to send |
| `group_id` | `str` | Yes | Group internal ID or display name |

```python
send_message_to_group(message="Team update", group_id="#dev-team")
```

## `send_reaction_to_user`

React to a user's message with an emoji.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `emoji` | `str` | Yes | The emoji to react with |
| `user_id` | `str` | Yes | Recipient phone number |
| `target_author` | `str` | Yes | Author of the message being reacted to |
| `target_timestamp` | `int` | Yes | Timestamp of the target message |
| `remove` | `bool` | No | Set `True` to remove a reaction (default: `False`) |

```python
send_reaction_to_user(
    emoji="👍",
    user_id="+15551234567",
    target_author="+15551234567",
    target_timestamp=1744185565466,
)
```

## `send_reaction_to_group`

React to a message in a group with an emoji.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `emoji` | `str` | Yes | The emoji to react with |
| `group_id` | `str` | Yes | Group internal ID or display name |
| `target_author` | `str` | Yes | Author of the message being reacted to |
| `target_timestamp` | `int` | Yes | Timestamp of the target message |
| `remove` | `bool` | No | Set `True` to remove a reaction (default: `False`) |

## `receive_message`

Wait for and receive the next actionable message (text or reaction) within a timeout. Messages that arrived while the daemon was streaming are queued, so back-to-back calls won't drop anything.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `timeout` | `float` | No | Seconds to wait (default: `60`) |

Returns a `MessageResponse` with either:

- **Text message**: `message`, `sender_id`, `sender_name`, `group_id`, `timestamp`
- **Reaction**: `reaction` (emoji, target_author, target_timestamp, is_remove)
- **Timeout**: empty response (all fields `None`)

An @-mentioned account also appears in `mentions` (uuid, UTF-16 `start`/`length`,
`name` when known), and the same mention is written into `message` where it
stood, as `@[<uuid>]`.

```python
result = await receive_message(timeout=30.0)
if result.message:
    print(f"Got: {result.message} from {result.sender_id}")
elif result.reaction:
    print(f"Got reaction: {result.reaction.emoji}")
```

## `a2ui_action`

Handle an interaction with an A2UI surface this server rendered. A2UI-capable hosts deliver Button presses on the chat surfaces as a call to this tool. It is dispatched exclusively through the same send paths as the tools above and enforces the same trusted-recipients allowlist — an action targeting an untrusted recipient fails exactly like the direct tool call.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | `str` | Yes | Action name carried by the button (`reply` or `react`) |
| `context` | `dict` | No | Button context with host-resolved bindings: `conversation_id`, plus `target_author` / `target_timestamp` for `react` (optionally `emoji`, default 👍, and `remove`) and `message` for `reply` |

The `context` is host-supplied data: it names a target, it does not grant permission to message it.

## Mentions

Signal keeps a mention in `bodyRanges` rather than in the body text, where the
`@name` is replaced by a placeholder character. Every send tool understands the
inline form `@[<uuid>]`: write it where the mention should read and the message
goes out as a real Signal mention of that account, with signal-mcp building the
`bodyRanges` entry and its UTF-16 offsets. Text that is not an exact canonical
UUID in that form (a bare `@name`, or a `[<uuid>]` without the `@`) is sent as
ordinary text. A received body can be sent straight back to mention the same
accounts again.

## `mark_read`

Mark a received message as read in Signal. In channel mode this happens automatically when a message is forwarded. In normal (polling) mode, call this after `receive_message` to send a read receipt.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sender` | `str` | Yes | Sender phone number (from `sender_id` in the received message) |
| `target_timestamp` | `int` | Yes | Message timestamp (from `timestamp` in the received message) |

```python
result = await mark_read(
    sender="+15551234567",
    target_timestamp=1744185565466,
)
```
