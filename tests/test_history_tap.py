"""Tests for the inbound history buffer tap in rpc.py (#59)."""

import asyncio
import json
import logging
from unittest.mock import patch

from signal_mcp.config import config
from signal_mcp.history import ConversationBuffer
from signal_mcp.rpc import SignalRpcClient


def _receive_notification(payload: dict) -> bytes:
    """Build a raw JSON-RPC receive notification line."""
    return (
        json.dumps({"jsonrpc": "2.0", "method": "receive", "params": payload}) + "\n"
    ).encode()


def _envelope(source: str, **kw) -> dict:
    return {
        "envelope": {"source": source, "sourceName": "Sender", **kw},
        "account": source,
    }


def _drain_queue(client: SignalRpcClient) -> list:
    """Drain all messages from the client's queue."""
    msgs = []
    while not client._messages.empty():
        msgs.append(client._messages.get_nowait())
    return msgs


class FakeReader:
    """asyncio.StreamReader stand-in: readline() drains a fed queue."""

    def __init__(self) -> None:
        self._lines: list[bytes] = []
        self._pos = 0

    def feed_data(self, data: bytes) -> None:
        for line in data.split(b"\n"):
            if line:
                self._lines.append(line + b"\n")
        self._lines.append(b"")  # EOF

    async def readline(self) -> bytes:
        if self._pos >= len(self._lines):
            return b""
        line = self._lines[self._pos]
        self._pos += 1
        return line


def test_tap_records_into_buffer():
    """The tap records each parsed envelope into the buffer."""
    buf = ConversationBuffer()

    async def scenario():
        client = SignalRpcClient("fake", 0)
        reader = FakeReader()
        reader.feed_data(
            _receive_notification(
                _envelope(
                    "+11234567890",
                    dataMessage={"message": "hello", "timestamp": 1000},
                )
            )
        )
        client._reader = reader  # type: ignore[assignment]
        with (
            patch.object(config, "account", "+15551234567"),
            patch.object(config, "trusted_senders", frozenset()),
            patch("signal_mcp.history.buffer", buf),
        ):
            await client._read_loop()

    asyncio.run(scenario())
    snap = buf.snapshot("+11234567890")
    assert len(snap) == 1
    assert snap[0].text == "hello"


def test_tap_does_not_consume_queue():
    """The message still reaches the receive queue exactly once."""

    async def scenario():
        client = SignalRpcClient("fake", 0)
        reader = FakeReader()
        reader.feed_data(
            _receive_notification(
                _envelope(
                    "+11234567890",
                    dataMessage={"message": "hello", "timestamp": 1000},
                )
            )
        )
        client._reader = reader  # type: ignore[assignment]
        with (
            patch.object(config, "account", "+15551234567"),
            patch.object(config, "trusted_senders", frozenset()),
        ):
            await client._read_loop()
        return client

    client = asyncio.run(scenario())
    from signal_mcp.rpc import _Disconnect

    all_msgs = _drain_queue(client)
    msgs = [m for m in all_msgs if not isinstance(m, _Disconnect)]
    assert len(msgs) == 1
    assert msgs[0].message == "hello"


def test_tap_failure_does_not_break_delivery(caplog):
    """When the tap raises, delivery still works and a warning is logged."""

    async def scenario():
        client = SignalRpcClient("fake", 0)
        reader = FakeReader()
        reader.feed_data(
            _receive_notification(
                _envelope(
                    "+11234567890",
                    dataMessage={"message": "hello", "timestamp": 1000},
                )
            )
        )
        client._reader = reader  # type: ignore[assignment]
        with (
            patch.object(config, "account", "+15551234567"),
            patch.object(config, "trusted_senders", frozenset()),
            patch(
                "signal_mcp.rpc.record_response",
                side_effect=RuntimeError("boom"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            await client._read_loop()
        return client

    client = asyncio.run(scenario())
    from signal_mcp.rpc import _Disconnect

    all_msgs = _drain_queue(client)
    msgs = [m for m in all_msgs if not isinstance(m, _Disconnect)]
    assert len(msgs) == 1
    assert msgs[0].message == "hello"
    assert any("Failed to record" in r.message for r in caplog.records)


def test_tap_queue_ordering_preserved():
    """A burst of envelopes preserves queue ordering."""

    async def scenario():
        client = SignalRpcClient("fake", 0)
        reader = FakeReader()
        data = b""
        for i in range(5):
            data += _receive_notification(
                _envelope(
                    "+11234567890",
                    dataMessage={"message": f"msg-{i}", "timestamp": 1000 + i},
                )
            )
        reader.feed_data(data)
        client._reader = reader  # type: ignore[assignment]
        with (
            patch.object(config, "account", "+15551234567"),
            patch.object(config, "trusted_senders", frozenset()),
        ):
            await client._read_loop()
        return client

    client = asyncio.run(scenario())
    from signal_mcp.rpc import _Disconnect

    all_msgs = _drain_queue(client)
    msgs = [m for m in all_msgs if not isinstance(m, _Disconnect)]
    assert [m.message for m in msgs] == [f"msg-{i}" for i in range(5)]
