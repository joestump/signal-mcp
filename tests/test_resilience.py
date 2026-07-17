"""Tests for channel forwarder and RPC client resilience (#12).

Covers the daemon-outage failure modes found in review:

- the forwarder survives a daemon that is unreachable at startup (retry with
  backoff instead of dying),
- teardown wakes a blocked ``next_message`` caller promptly instead of letting
  it wait out its receive timeout, and the client reconnects afterwards,
- a write that fails mid-call surfaces as ``SignalCLIError`` and cleans up its
  pending future,
- ``close()`` cancels the reader task and tears the connection down, and
- ``receive_message`` refuses to run in channel mode (single-consumer rule).

The RPC client is driven against in-memory fake streams (no sockets); the
forwarder is driven with the ``FakeClient`` pattern from test_channel.
"""

import asyncio
import json

import pytest

from signal_mcp import channel, rpc
from signal_mcp.channel import _forward_channel_messages
from signal_mcp.config import config
from signal_mcp.parse import MessageResponse
from signal_mcp.rpc import SignalCLIError, SignalError, SignalRpcClient
from signal_mcp.tools import receive_message


# ---------------------------------------------------------------------------
# In-memory fake streams for driving SignalRpcClient without a real socket
# ---------------------------------------------------------------------------


class FakeReader:
    """asyncio.StreamReader stand-in: readline() drains a fed queue.

    Feed ``b""`` to simulate the daemon closing the connection (EOF).
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    def feed(self, line: bytes) -> None:
        self._queue.put_nowait(line)

    async def readline(self) -> bytes:
        return await self._queue.get()


class FakeWriter:
    """asyncio.StreamWriter stand-in: records writes, tracks close state."""

    def __init__(self) -> None:
        self._closing = False
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True


def _install_fake_connection(monkeypatch):
    """Patch asyncio.open_connection to hand out fresh fake stream pairs.

    Returns the list that each (reader, writer) pair is appended to, so a test
    can drive the current connection and observe reconnects.
    """
    conns: list[tuple[FakeReader, FakeWriter]] = []

    async def fake_open(host, port):
        pair = (FakeReader(), FakeWriter())
        conns.append(pair)
        return pair

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    return conns


def _receive_line(text: str, sender: str = "+15551234567", ts: int = 5) -> bytes:
    """A JSON-RPC ``receive`` notification line for a plain text message."""
    payload = {
        "method": "receive",
        "params": {
            "envelope": {
                "source": sender,
                "dataMessage": {"message": text, "timestamp": ts},
            }
        },
    }
    return (json.dumps(payload) + "\n").encode()


# ---------------------------------------------------------------------------
# Teardown wakes a blocked next_message; the client reconnects afterwards
# ---------------------------------------------------------------------------


def test_teardown_wakes_blocked_next_message_promptly(monkeypatch):
    """A disconnect wakes a caller blocked on a long timeout, returning None."""
    _install_fake_connection(monkeypatch)

    async def scenario():
        client = SignalRpcClient("daemon", 7583)
        await client.connect()

        # Block on a deliberately huge timeout — must NOT wait it out.
        waiter = asyncio.create_task(client.next_message(timeout=3600))
        await asyncio.sleep(0.01)
        assert not waiter.done()

        client._teardown(SignalCLIError("daemon connection closed"))

        result = await asyncio.wait_for(waiter, timeout=1)
        assert result is None

    asyncio.run(scenario())


def test_eof_disconnect_then_reconnect(monkeypatch):
    """After the daemon drops (EOF), the next call reconnects and delivers."""
    conns = _install_fake_connection(monkeypatch)

    async def scenario():
        client = SignalRpcClient("daemon", 7583)
        await client.connect()
        reader0, _ = conns[0]

        reader0.feed(_receive_line("first"))
        msg = await asyncio.wait_for(client.next_message(timeout=1), timeout=1)
        assert msg is not None and msg.message == "first"

        # Daemon closes the connection.
        reader0.feed(b"")
        # A blocked caller wakes promptly via the disconnect sentinel.
        assert (
            await asyncio.wait_for(client.next_message(timeout=3600), timeout=1) is None
        )

        # The next call transparently reconnects (a second stream pair opens).
        second = asyncio.create_task(client.next_message(timeout=1))
        await asyncio.sleep(0.01)
        assert len(conns) == 2
        reader1, _ = conns[1]
        reader1.feed(_receive_line("after-reconnect"))
        msg2 = await asyncio.wait_for(second, timeout=1)
        assert msg2 is not None and msg2.message == "after-reconnect"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Write-path errors surface as SignalCLIError and clean up the pending future
# ---------------------------------------------------------------------------


def test_write_failure_surfaces_as_signalclierror(monkeypatch):
    """A broken socket on write() raises SignalCLIError; no future leaks."""
    _install_fake_connection(monkeypatch)

    async def scenario():
        client = SignalRpcClient("daemon", 7583)
        await client.connect()

        def boom(_data):
            raise ConnectionResetError("broken pipe")

        assert client._writer is not None
        monkeypatch.setattr(client._writer, "write", boom)

        with pytest.raises(SignalCLIError, match="Failed to send"):
            await client.call("send", {"message": "hi"}, timeout=1)

        # The pending future was cleaned up rather than left to leak/deadlock.
        assert client._pending == {}

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Graceful close()
# ---------------------------------------------------------------------------


def test_close_cancels_reader_and_tears_down(monkeypatch):
    """close() cancels the reader task and releases the writer."""
    _install_fake_connection(monkeypatch)

    async def scenario():
        client = SignalRpcClient("daemon", 7583)
        await client.connect()
        reader_task = client._reader_task
        assert reader_task is not None

        await client.close()

        assert reader_task.done()
        assert client._reader_task is None
        assert client._writer is None

    asyncio.run(scenario())


def test_close_is_safe_without_a_connection():
    """close() on a never-connected client is a harmless no-op."""

    async def scenario():
        client = SignalRpcClient("daemon", 7583)
        await client.close()  # must not raise
        assert client._writer is None

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Forwarder survives an initially-down daemon (retry with backoff)
# ---------------------------------------------------------------------------


class FlakyClient:
    """next_message raises SignalCLIError ``fail_times`` times, then yields."""

    def __init__(self, fail_times: int, message: MessageResponse) -> None:
        self._remaining = fail_times
        self._message = message
        self._delivered = False
        self.attempts = 0
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        return {"timestamp": 1}

    async def next_message(self, timeout: float):
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise SignalCLIError("daemon down at startup")
        if not self._delivered:
            self._delivered = True
            return self._message
        await asyncio.sleep(3600)  # nothing more; block until cancelled


def test_forwarder_retries_until_daemon_available(monkeypatch):
    """The forwarder does not die when the daemon is down at startup."""
    # Make the backoff effectively instant so the test is fast.
    monkeypatch.setattr(channel, "_INITIAL_BACKOFF", 0.001)
    monkeypatch.setattr(channel, "_MAX_BACKOFF", 0.001)
    monkeypatch.setattr(config, "prefix", "")

    message = MessageResponse(message="finally", sender_id="+15551234567", timestamp=1)
    flaky = FlakyClient(fail_times=3, message=message)

    class Stream:
        def __init__(self):
            self.sent = []

        async def send(self, msg):
            self.sent.append(msg)

    stream = Stream()

    async def scenario():
        monkeypatch.setattr(rpc, "client", flaky)
        task = asyncio.create_task(_forward_channel_messages(stream))
        # Poll until the message makes it through (after 3 failed attempts).
        for _ in range(200):
            if stream.sent:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert flaky._remaining == 0  # all failures were retried
    assert len(stream.sent) == 1
    assert stream.sent[0].root.params["content"] == "finally"


# ---------------------------------------------------------------------------
# Single consumer in channel mode
# ---------------------------------------------------------------------------


def test_receive_message_refuses_in_channel_mode(monkeypatch):
    """In channel mode receive_message errors instead of stealing messages."""
    monkeypatch.setattr(config, "channel_mode", True)

    with pytest.raises(SignalError, match="channel mode"):
        asyncio.run(receive_message(timeout=0))


def test_receive_message_allowed_when_not_channel_mode(monkeypatch):
    """Outside channel mode receive_message still polls (returns on timeout)."""
    monkeypatch.setattr(config, "channel_mode", False)

    class IdleClient:
        async def next_message(self, timeout: float):
            return None

    monkeypatch.setattr(rpc, "client", IdleClient())
    result = asyncio.run(receive_message(timeout=0))
    assert isinstance(result, MessageResponse)
    assert result.message is None
