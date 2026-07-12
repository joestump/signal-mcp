"""Tests for Claude Channel mode: prefix filtering and notification building."""

import asyncio
from unittest.mock import patch

from mcp.types import JSONRPCNotification

from signal_mcp.main import (
    MessageResponse,
    Reaction,
    _forward_channel_messages,
    config,
)


class FakeWriteStream:
    """Minimal stand-in for the MCP write stream — collects sent messages."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, msg) -> None:
        self.sent.append(msg)


class FakeClient:
    """Yields pre-loaded messages from next_message()."""

    def __init__(self, messages: list) -> None:
        self._messages = list(messages)

    async def _ensure_connected(self) -> None:
        pass

    async def next_message(self, timeout: float):
        if not self._messages:
            await asyncio.sleep(3600)
        return self._messages.pop(0)


def _text_msg(text: str, sender: str = "+1234", group: str | None = None):
    return MessageResponse(message=text, sender_id=sender, group_name=group)


def _run_forwarder(messages, prefix=""):
    """Run the forwarder just long enough to drain queued messages, then cancel."""
    config.prefix = prefix
    fake_client = FakeClient(messages)
    stream = FakeWriteStream()

    async def _runner():
        with patch("signal_mcp.main._client", return_value=fake_client):
            task = asyncio.create_task(_forward_channel_messages(stream))
            await asyncio.sleep(0.15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return stream.sent

    return asyncio.run(_runner())


def test_forwards_text_message():
    """A plain text message is forwarded as a channel notification."""
    sent = _run_forwarder([_text_msg("hello world", sender="+15551234567")])
    assert len(sent) == 1
    notif = sent[0].root
    assert isinstance(notif, JSONRPCNotification)
    assert notif.method == "notifications/claude/channel"
    assert notif.params["content"] == "hello world"
    assert notif.params["meta"]["sender"] == "+15551234567"
    assert "group" not in notif.params["meta"]


def test_forwards_group_message_with_meta():
    """Group messages include the group id in meta."""
    sent = _run_forwarder([_text_msg("hi team", sender="+111", group="group-123==")])
    assert len(sent) == 1
    assert sent[0].root.params["meta"]["group"] == "group-123=="


def test_skips_non_text_messages():
    """Reactions (message=None) are not forwarded."""
    reaction = MessageResponse(
        sender_id="+1234",
        reaction=Reaction(
            emoji="\U0001f44d", target_author="+1234", target_timestamp=1
        ),
    )
    sent = _run_forwarder([reaction])
    assert len(sent) == 0


def test_prefix_filters_and_strips():
    """Only messages matching the prefix are forwarded, with prefix stripped."""
    sent = _run_forwarder(
        [
            _text_msg("cc run tests"),
            _text_msg("buy milk"),
            _text_msg("CC deploy now"),
        ],
        prefix="cc",
    )
    assert len(sent) == 2
    assert sent[0].root.params["content"] == "run tests"
    assert sent[1].root.params["content"] == "deploy now"


def test_notification_is_valid_jsonrpc():
    """The notification serializes to valid JSON-RPC 2.0."""
    sent = _run_forwarder([_text_msg("test")])
    raw = sent[0].model_dump(by_alias=True, exclude_none=True)
    assert raw["jsonrpc"] == "2.0"
    assert raw["method"] == "notifications/claude/channel"
    assert "content" in raw["params"]
    assert "meta" in raw["params"]
