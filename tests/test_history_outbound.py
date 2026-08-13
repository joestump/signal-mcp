"""Tests for outbound history buffer recording in tools.py (#60)."""

import asyncio
import logging
from unittest.mock import patch

from signal_mcp.config import config
from signal_mcp.history import ConversationBuffer
from signal_mcp.rpc import SignalCLIError
from signal_mcp.tools import _send_message, _send_reaction
from signal_mcp import rpc

ACCOUNT = "+15551234567"
OTHER = "+11234567890"
GROUP_ID = "dGVhbQ=="


class FakeClient:
    """Stand-in for SignalRpcClient that records calls and returns timestamps."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.next_timestamp = 1700000000000
        self.fail = False

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        if self.fail:
            raise SignalCLIError("simulated failure")
        self.next_timestamp += 1000
        return {"timestamp": self.next_timestamp}


def test_outbound_dm_recorded():
    """A successful DM send is recorded in the buffer with the daemon-returned timestamp."""
    fake = FakeClient()
    buf = ConversationBuffer()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "account", ACCOUNT),
        patch("signal_mcp.tools.history_buffer", buf),
    ):
        asyncio.run(_send_message("reply", OTHER))

    snap = buf.snapshot(OTHER)
    assert len(snap) == 1
    assert snap[0].direction == "outbound"
    assert snap[0].sender_id == ACCOUNT
    assert snap[0].text == "reply"
    assert snap[0].timestamp == fake.next_timestamp


def test_outbound_group_recorded():
    """A successful group send is recorded under the group id."""
    fake = FakeClient()
    buf = ConversationBuffer()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "account", ACCOUNT),
        patch("signal_mcp.tools.history_buffer", buf),
    ):
        asyncio.run(_send_message("team update", GROUP_ID, is_group=True))

    snap = buf.snapshot(GROUP_ID)
    assert len(snap) == 1
    assert snap[0].direction == "outbound"


def test_outbound_reaction_recorded():
    """A successful reaction send is recorded via record_reaction."""
    fake = FakeClient()
    buf = ConversationBuffer()
    # Seed the buffer with the target message.
    from signal_mcp.history import BufferedMessage

    buf.record(
        BufferedMessage(
            conversation_key=OTHER,
            direction="inbound",
            sender_id=OTHER,
            sender_name="Bob",
            text="hello",
            timestamp=1000,
        )
    )
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "account", ACCOUNT),
        patch("signal_mcp.tools.history_buffer", buf),
    ):
        asyncio.run(_send_reaction("\U0001f44d", OTHER, OTHER, 1000))

    snap = buf.snapshot(OTHER)
    # The message should now have a reaction from the account.
    assert len(snap[0].reactions) == 1
    assert snap[0].reactions[0].emoji == "\U0001f44d"
    assert snap[0].reactions[0].author == ACCOUNT


def test_failed_send_records_nothing():
    """A failed RPC records nothing in the buffer."""
    fake = FakeClient()
    fake.fail = True
    buf = ConversationBuffer()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "account", ACCOUNT),
        patch("signal_mcp.tools.history_buffer", buf),
    ):
        try:
            asyncio.run(_send_message("reply", OTHER))
        except SignalCLIError:
            pass

    assert buf.snapshot(OTHER) == []


def test_buffer_failure_does_not_break_send(caplog):
    """A raising buffer does not fail the send."""
    fake = FakeClient()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "account", ACCOUNT),
        patch(
            "signal_mcp.tools.conversation_key",
            side_effect=RuntimeError("buffer boom"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        # Should not raise — the send itself succeeds.
        asyncio.run(_send_message("reply", OTHER))

    assert any("Failed to record" in r.message for r in caplog.records)
