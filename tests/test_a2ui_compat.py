"""Compatibility regression tests — existing tools unchanged after A2UI (#68)."""

import asyncio
from unittest.mock import patch

from signal_mcp import rpc
from signal_mcp.config import config
from signal_mcp.history import ConversationBuffer
from signal_mcp.parse import MessageResponse
from signal_mcp.tools import (
    mark_read,
    receive_message,
    send,
    send_message_to_group,
    send_message_to_user,
    send_reaction_to_group,
    send_reaction_to_user,
)

ACCOUNT = "+15551234567"
OTHER = "+11234567890"


class FakeClient:
    """Stand-in for SignalRpcClient."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._messages: asyncio.Queue = asyncio.Queue()
        self.next_timestamp = 1700000000000

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        self.next_timestamp += 1000
        return {"timestamp": self.next_timestamp}

    async def next_message(self, timeout):
        try:
            return await asyncio.wait_for(self._messages.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return None

    async def connect(self):
        pass

    async def close(self):
        pass


def test_send_returns_same_shape():
    """send() returns {"message": "Message sent successfully"} — unchanged."""
    fake = FakeClient()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "account", ACCOUNT),
        patch.object(config, "operator", OTHER),
        patch.object(config, "trusted_recipients", frozenset()),
    ):
        result = asyncio.run(send("hello"))
    assert result == {"message": "Message sent successfully"}


def test_send_message_to_user_returns_same_shape():
    fake = FakeClient()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "trusted_recipients", frozenset()),
    ):
        result = asyncio.run(send_message_to_user("hi", OTHER))
    assert result == {"message": "Message sent successfully"}


def test_send_message_to_group_returns_same_shape():
    fake = FakeClient()
    fake.calls.clear()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "trusted_recipients", frozenset()),
    ):
        # Patch _resolve_group to avoid a daemon call.
        async def fake_resolve(name):
            return {"id": "GID==", "name": name}

        with patch("signal_mcp.tools._resolve_group", fake_resolve):
            result = asyncio.run(send_message_to_group("hi", "GID=="))
    assert result == {"message": "Message sent successfully"}


def test_send_reaction_to_user_returns_same_shape():
    fake = FakeClient()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "trusted_recipients", frozenset()),
    ):
        result = asyncio.run(send_reaction_to_user("\U0001f44d", OTHER, OTHER, 1000))
    assert result == {"message": "Reaction sent successfully"}


def test_send_reaction_to_group_returns_same_shape():
    fake = FakeClient()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "trusted_recipients", frozenset()),
    ):

        async def fake_resolve(name):
            return {"id": "GID==", "name": name}

        with patch("signal_mcp.tools._resolve_group", fake_resolve):
            result = asyncio.run(
                send_reaction_to_group("\U0001f44d", "GID==", OTHER, 1000)
            )
    assert result == {"message": "Reaction sent successfully"}


def test_receive_message_returns_message_response():
    """receive_message returns a MessageResponse — same type as before."""
    fake = FakeClient()
    msg = MessageResponse(message="hello", sender_id=OTHER, timestamp=1000)
    fake._messages.put_nowait(msg)
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "trusted_senders", frozenset()),
        patch.object(config, "channel_mode", False),
    ):
        result = asyncio.run(receive_message(timeout=1))
    assert isinstance(result, MessageResponse)
    assert result.message == "hello"


def test_mark_read_returns_same_shape():
    fake = FakeClient()
    with (
        patch.object(rpc, "client", fake),
    ):
        result = asyncio.run(mark_read(OTHER, 1000))
    assert result == {"message": "Read receipt sent"}


def test_a2ui_resources_have_audience_user():
    """A2UI resources declare audience: ['user'] while tools do not."""
    from signal_mcp.tools import mcp

    rm = mcp._resource_manager
    for uri, template in rm._templates.items():
        assert template.annotations is not None, uri
        assert template.annotations.audience is not None, uri
        assert "user" in template.annotations.audience, uri
    for uri, resource in rm._resources.items():
        assert resource.annotations is not None, uri
        assert resource.annotations.audience is not None, uri
        assert "user" in resource.annotations.audience, uri


def test_no_resources_host_path_unchanged():
    """A host that never reads resources exercises tools end-to-end identically."""
    fake = FakeClient()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "account", ACCOUNT),
        patch.object(config, "operator", OTHER),
        patch.object(config, "trusted_recipients", frozenset()),
        patch("signal_mcp.tools.history_buffer", ConversationBuffer()),
    ):
        # Exercise send + send_message_to_user — both should work.
        r1 = asyncio.run(send("text me"))
        r2 = asyncio.run(send_message_to_user("hi", OTHER))
    assert r1 == {"message": "Message sent successfully"}
    assert r2 == {"message": "Message sent successfully"}
