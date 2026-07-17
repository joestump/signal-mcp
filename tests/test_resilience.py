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
from signal_mcp.rpc import (
    SignalCLIError,
    SignalDisconnectedError,
    SignalError,
    SignalRpcClient,
)
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
    """A disconnect wakes a caller blocked on a long timeout, raising promptly."""
    _install_fake_connection(monkeypatch)

    async def scenario():
        client = SignalRpcClient("daemon", 7583)
        await client.connect()

        # Block on a deliberately huge timeout — must NOT wait it out.
        waiter = asyncio.create_task(client.next_message(timeout=3600))
        await asyncio.sleep(0.01)
        assert not waiter.done()

        client._teardown(SignalCLIError("daemon connection closed"))

        # A drop is distinguishable from a quiet idle period: it raises.
        with pytest.raises(SignalDisconnectedError):
            await asyncio.wait_for(waiter, timeout=1)

    asyncio.run(scenario())


def test_eof_disconnect_then_reconnect(monkeypatch):
    """After the daemon drops (EOF) with no blocked waiter, the next call
    reconnects transparently and delivers — no spurious disconnect surfaces."""
    conns = _install_fake_connection(monkeypatch)

    async def scenario():
        client = SignalRpcClient("daemon", 7583)
        await client.connect()
        reader0, _ = conns[0]

        reader0.feed(_receive_line("first"))
        msg = await asyncio.wait_for(client.next_message(timeout=1), timeout=1)
        assert msg is not None and msg.message == "first"

        # Daemon closes the connection while nobody is blocked in next_message.
        reader0.feed(b"")
        await asyncio.sleep(0.02)  # let the reader hit EOF and tear down

        # The next call reconnects (a second stream pair) and delivers. It does
        # NOT raise: the reconnect succeeded, so the stale disconnect is drained.
        second = asyncio.create_task(client.next_message(timeout=1))
        await asyncio.sleep(0.01)
        assert len(conns) == 2
        reader1, _ = conns[1]
        reader1.feed(_receive_line("after-reconnect"))
        msg2 = await asyncio.wait_for(second, timeout=1)
        assert msg2 is not None and msg2.message == "after-reconnect"

    asyncio.run(scenario())


def test_stale_disconnect_sentinel_drained_after_queued_message(monkeypatch):
    """A drop with a message queued ahead of the sentinel must not surface a
    spurious disconnect after that message is delivered and we reconnect."""
    conns = _install_fake_connection(monkeypatch)

    async def scenario():
        client = SignalRpcClient("daemon", 7583)
        await client.connect()
        reader0, _ = conns[0]

        # A message arrives, then the daemon drops: the queue holds the message
        # followed by the disconnect sentinel enqueued by teardown.
        reader0.feed(_receive_line("queued"))
        reader0.feed(b"")
        await asyncio.sleep(0.02)

        # First call reconnects, drains the stale sentinel, and returns the msg.
        msg = await asyncio.wait_for(client.next_message(timeout=1), timeout=1)
        assert msg is not None and msg.message == "queued"

        # The follow-up call must idle out (None), NOT raise a stale disconnect.
        result = await asyncio.wait_for(client.next_message(timeout=0.05), timeout=1)
        assert result is None

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


class DisconnectingClient:
    """next_message raises SignalDisconnectedError ``drops`` times, then delivers
    one message, then blocks forever (independent of asyncio.sleep patching)."""

    def __init__(self, drops: int, message: MessageResponse) -> None:
        self._drops = drops
        self._message = message
        self._delivered = False
        self._idle = asyncio.Event()
        self.attempts = 0
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        return {"timestamp": 1}

    async def next_message(self, timeout: float):
        self.attempts += 1
        if self._drops > 0:
            self._drops -= 1
            raise SignalDisconnectedError("daemon dropped mid-wait")
        if not self._delivered:
            self._delivered = True
            return self._message
        await self._idle.wait()  # block until cancelled


def test_forwarder_backs_off_on_repeated_disconnects_no_hot_spin(monkeypatch):
    """A flapping daemon (repeated drops) makes the forwarder back off — with
    growth and a cap — instead of hot-looping through instant reconnects."""
    monkeypatch.setattr(channel, "_INITIAL_BACKOFF", 1.0)
    monkeypatch.setattr(channel, "_MAX_BACKOFF", 4.0)
    monkeypatch.setattr(config, "prefix", "")

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        await real_sleep(0)  # yield, but don't actually wait

    # channel imports asyncio as a module, so this records the forwarder's sleeps.
    monkeypatch.setattr(channel.asyncio, "sleep", recording_sleep)

    message = MessageResponse(message="up", sender_id="+15551234567", timestamp=1)
    client = DisconnectingClient(drops=4, message=message)

    class Stream:
        def __init__(self):
            self.sent = []

        async def send(self, msg):
            self.sent.append(msg)

    stream = Stream()

    async def scenario():
        monkeypatch.setattr(rpc, "client", client)
        task = asyncio.create_task(_forward_channel_messages(stream))
        for _ in range(1000):
            if stream.sent:
                break
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    # Each disconnect sleeps before retrying (no hot spin), doubling 1→2→4 and
    # then saturating at the 4s cap — never reset between consecutive drops.
    assert sleeps[:4] == [1.0, 2.0, 4.0, 4.0]
    assert len(stream.sent) == 1


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


# ---------------------------------------------------------------------------
# JSON-RPC call() round-trip: success, error response, timeout
# ---------------------------------------------------------------------------


def test_call_returns_result_on_matching_response(monkeypatch):
    """call() routes the reader's response back to the caller by id."""
    conns = _install_fake_connection(monkeypatch)

    async def scenario():
        client = SignalRpcClient("daemon", 7583)
        await client.connect()
        reader0, _ = conns[0]

        async def respond():
            await asyncio.sleep(0.02)  # let call() register _pending[1] first
            reader0.feed(
                (
                    json.dumps({"jsonrpc": "2.0", "id": 1, "result": [{"id": "G"}]})
                    + "\n"
                ).encode()
            )

        asyncio.create_task(respond())
        result = await asyncio.wait_for(client.call("listGroups"), timeout=1)
        assert result == [{"id": "G"}]

    asyncio.run(scenario())


def test_call_raises_on_error_response(monkeypatch):
    """An {"error": ...} response surfaces as SignalCLIError."""
    conns = _install_fake_connection(monkeypatch)

    async def scenario():
        client = SignalRpcClient("daemon", 7583)
        await client.connect()
        reader0, _ = conns[0]

        async def respond():
            await asyncio.sleep(0.02)
            reader0.feed(
                (
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "msg": "no"}}
                    )
                    + "\n"
                ).encode()
            )

        asyncio.create_task(respond())
        with pytest.raises(SignalCLIError):
            await asyncio.wait_for(client.call("send"), timeout=1)

    asyncio.run(scenario())


def test_call_times_out_without_response(monkeypatch):
    """call() times out and cleans up its pending future when no reply comes."""
    _install_fake_connection(monkeypatch)

    async def scenario():
        client = SignalRpcClient("daemon", 7583)
        await client.connect()
        with pytest.raises(SignalCLIError, match="timed out"):
            await client.call("send", timeout=0.05)
        assert client._pending == {}

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Forwarder survives non-daemon errors without dropping/crashing
# ---------------------------------------------------------------------------


class _SeqClient:
    """Delivers a fixed message list, then blocks (Event, sleep-patch immune).

    ``receipt_fails`` makes every sendReceipt call raise.
    """

    def __init__(self, messages, receipt_fails=False):
        self._messages = list(messages)
        self._receipt_fails = receipt_fails
        self._idle = asyncio.Event()
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        if self._receipt_fails and method == "sendReceipt":
            raise SignalCLIError("receipt failed")
        return {"timestamp": 1}

    async def next_message(self, timeout: float):
        if self._messages:
            return self._messages.pop(0)
        await self._idle.wait()


def _drain_forwarder(client, stream, monkeypatch, real_sleep):
    async def scenario():
        monkeypatch.setattr(rpc, "client", client)
        task = asyncio.create_task(_forward_channel_messages(stream))
        for _ in range(1000):
            if stream.sent:
                break
            await real_sleep(0)
        await real_sleep(0.02)  # let any trailing receipt attempt run
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


class _CollectStream:
    def __init__(self):
        self.sent: list = []

    async def send(self, msg):
        self.sent.append(msg)


def test_forwarder_survives_receipt_failure(monkeypatch):
    """A failing read receipt neither drops the message nor kills the forwarder."""
    monkeypatch.setattr(config, "prefix", "")
    msg = MessageResponse(message="hi", sender_id="+15551234567", timestamp=1)
    client = _SeqClient([msg], receipt_fails=True)
    stream = _CollectStream()

    _drain_forwarder(client, stream, monkeypatch, asyncio.sleep)

    assert len(stream.sent) == 1  # delivered despite the receipt failure
    assert any(c[0] == "sendReceipt" for c in client.calls)


def test_forwarder_survives_notification_send_error(monkeypatch):
    """A transient write_stream.send error is caught; the forwarder continues."""
    monkeypatch.setattr(config, "prefix", "")
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(channel.asyncio, "sleep", fast_sleep)

    m1 = MessageResponse(message="one", sender_id="+111", timestamp=1)
    m2 = MessageResponse(message="two", sender_id="+111", timestamp=2)
    client = _SeqClient([m1, m2])

    class FlakyStream:
        def __init__(self):
            self.sent: list = []
            self._failed = False

        async def send(self, msg):
            if not self._failed:
                self._failed = True
                raise RuntimeError("wedged pipe")
            self.sent.append(msg)

    stream = FlakyStream()
    _drain_forwarder(client, stream, monkeypatch, real_sleep)

    # The first message's send raised (and was dropped); the loop recovered and
    # delivered the second — the forwarder did not crash.
    assert [n.root.params["content"] for n in stream.sent] == ["two"]


# ---------------------------------------------------------------------------
# Group resolution failure surfaces cleanly
# ---------------------------------------------------------------------------


def test_send_to_group_errors_when_listgroups_fails(monkeypatch):
    """When listGroups fails the group cannot be resolved, so the send errors."""
    from signal_mcp.tools import send_message_to_group

    monkeypatch.setattr(config, "trusted_recipients", frozenset())

    class FailingGroups:
        async def call(self, method, params=None, timeout=30.0):
            raise SignalCLIError("daemon down")

    monkeypatch.setattr(rpc, "client", FailingGroups())
    with pytest.raises(SignalError, match="Could not find group"):
        asyncio.run(send_message_to_group("hi", "some-group"))
