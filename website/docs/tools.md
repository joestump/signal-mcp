---
sidebar_position: 3
---

# Tools

Signal MCP exposes the following MCP tools:

## `send`

Send a message to the channel owner's phone. No phone number needed — this uses the `--user-id` configured at startup.

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
| `timeout` | `float` | Yes | Seconds to wait |

Returns a `MessageResponse` with either:

- **Text message**: `message`, `sender_id`, `group_name`, `timestamp`
- **Reaction**: `reaction` (emoji, target_author, target_timestamp, is_remove)
- **Timeout**: empty response (all fields `None`)

```python
result = await receive_message(timeout=30.0)
if result.message:
    print(f"Got: {result.message} from {result.sender_id}")
elif result.reaction:
    print(f"Got reaction: {result.reaction.emoji}")
```

## `mark_read`

Mark a received message as read in Signal. In channel mode this happens automatically when a message is forwarded. In normal (polling) mode, call this after `receive_message` to send a read receipt.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sender` | `str` | Yes | Sender phone number (from `sender_id` in the received message) |
| `target_timestamp` | `int` | Yes | Message timestamp (from `timestamp` in the received message) |
| `group_id` | `str` | No | Group ID if the message was in a group |

```python
result = await mark_read(
    sender="+15551234567",
    target_timestamp=1744185565466,
)
```
