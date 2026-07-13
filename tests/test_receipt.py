"""Tests for read receipt functionality: _send_receipt, mark_read tool, and
channel forwarder auto-mark-as-read."""

import asyncio

from signal_mcp import main
from signal_mcp.main import (
    SignalCLIError,
    _send_receipt,
    mark_read,
)

OTHER = "+11234567890"
TIMESTAMP = 1744185565466


class FakeClient:
    """Stand-in for SignalRpcClient that records JSON-RPC calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params))
        return {"timestamp": 1}


class FailingClient:
    """FakeClient whose call() always raises SignalCLIError."""

    async def call(self, method, params=None, timeout=30.0):
        raise SignalCLIError("connection refused")


# ---------------------------------------------------------------------------
# _send_receipt helper tests
# ---------------------------------------------------------------------------


def test_send_receipt_direct_message(monkeypatch):
    """Direct message read receipt uses recipient param."""
    fake = FakeClient()
    monkeypatch.setattr(main, "client", fake)

    ok = asyncio.run(_send_receipt(OTHER, TIMESTAMP))
    assert ok is True
    method, params = fake.calls[-1]
    assert method == "sendReceipt"
    assert params["recipient"] == [OTHER]
    assert params["targetTimestamp"] == TIMESTAMP
    assert params["type"] == "read"


def test_send_receipt_always_uses_recipient_not_group(monkeypatch):
    """Read receipts always address the author, never a groupId."""
    fake = FakeClient()
    monkeypatch.setattr(main, "client", fake)

    ok = asyncio.run(_send_receipt(OTHER, TIMESTAMP))
    assert ok is True
    method, params = fake.calls[-1]
    assert "groupId" not in params
    assert params["recipient"] == [OTHER]


def test_send_receipt_coerces_timestamp_to_int(monkeypatch):
    """targetTimestamp is coerced to int even if a float is passed."""
    fake = FakeClient()
    monkeypatch.setattr(main, "client", fake)

    ok = asyncio.run(_send_receipt(OTHER, 1744185565466.5))
    assert ok is True
    method, params = fake.calls[-1]
    assert params["targetTimestamp"] == 1744185565466
    assert isinstance(params["targetTimestamp"], int)


def test_send_receipt_returns_false_on_error(monkeypatch):
    """A SignalCLIError from the daemon returns False."""
    monkeypatch.setattr(main, "client", FailingClient())

    ok = asyncio.run(_send_receipt(OTHER, TIMESTAMP))
    assert ok is False


# ---------------------------------------------------------------------------
# mark_read tool tests
# ---------------------------------------------------------------------------


def test_mark_read_success(monkeypatch):
    """mark_read delegates to _send_receipt and returns success."""
    fake = FakeClient()
    monkeypatch.setattr(main, "client", fake)

    result = asyncio.run(mark_read(OTHER, TIMESTAMP))
    assert result == {"message": "Read receipt sent"}


def test_mark_read_missing_sender():
    """mark_read returns an error when sender is empty."""
    result = asyncio.run(mark_read("", TIMESTAMP))
    assert "error" in result


def test_mark_read_missing_timestamp():
    """mark_read returns an error when timestamp is 0 (falsy)."""
    result = asyncio.run(mark_read(OTHER, 0))
    assert "error" in result


def test_mark_read_daemon_error(monkeypatch):
    """mark_read returns error dict when _send_receipt fails."""
    monkeypatch.setattr(main, "client", FailingClient())

    result = asyncio.run(mark_read(OTHER, TIMESTAMP))
    assert "error" in result
