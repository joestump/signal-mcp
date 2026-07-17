"""Tests for inbound trusted-sender gating.

Covers the shared ``is_trusted_sender`` helper, the channel-mode forwarder
(deny-by-default when no allowlist is configured), and the ``receive_message``
polling path (filtered only when an allowlist is configured).
"""

import asyncio
from unittest.mock import patch

import pytest

from signal_mcp import rpc
from signal_mcp.channel import _forward_channel_messages
from signal_mcp.config import _load_trusted_senders, config, is_trusted_sender
from signal_mcp.parse import MessageResponse
from signal_mcp.tools import receive_message

OWNER = "+15550001111"
ALICE = "+15555550101"
MALLORY = "+15555550199"
GROUP = "GID=="


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch):
    """Give every test a known config baseline (restored afterwards)."""
    monkeypatch.setattr(config, "user_id", OWNER)
    monkeypatch.setattr(config, "prefix", "")
    monkeypatch.setattr(config, "channel_mode", False)
    monkeypatch.setattr(config, "trusted_senders", frozenset())


class FakeWriteStream:
    """Minimal stand-in for the MCP write stream — collects sent messages."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, msg) -> None:
        self.sent.append(msg)


class FakeClient:
    """Yields pre-loaded messages from next_message() and records calls."""

    def __init__(self, messages: list) -> None:
        self._messages = list(messages)
        self.calls: list[tuple[str, dict]] = []

    async def connect(self) -> None:
        pass

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        return {"timestamp": 1}

    async def next_message(self, timeout: float):
        if not self._messages:
            await asyncio.sleep(timeout)
            return None
        return self._messages.pop(0)


def _text_msg(text, sender=ALICE, group=None, timestamp=1744185565466):
    return MessageResponse(
        message=text, sender_id=sender, group_id=group, timestamp=timestamp
    )


def _run_forwarder(messages):
    """Run the forwarder just long enough to drain queued messages, then cancel.

    Returns (sent_notifications, fake_client).
    """
    fake_client = FakeClient(messages)
    stream = FakeWriteStream()

    async def _runner():
        with patch.object(rpc, "client", fake_client):
            task = asyncio.create_task(_forward_channel_messages(stream))
            await asyncio.sleep(0.15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return stream.sent, fake_client

    return asyncio.run(_runner())


def _receipts(fake: FakeClient) -> list[dict]:
    return [params for method, params in fake.calls if method == "sendReceipt"]


# --- is_trusted_sender ------------------------------------------------------


def test_helper_allows_everyone_when_unconfigured_polling():
    """No allowlist + no channel mode: every author passes (unchanged)."""
    assert is_trusted_sender(MALLORY)
    assert is_trusted_sender(None)


def test_helper_channel_default_is_owner_only(monkeypatch):
    """Channel mode with no allowlist is deny-by-default: only user_id passes."""
    monkeypatch.setattr(config, "channel_mode", True)
    assert is_trusted_sender(OWNER)
    assert not is_trusted_sender(MALLORY)
    assert not is_trusted_sender(None)
    assert not is_trusted_sender("")


def test_helper_configured_list_is_exhaustive(monkeypatch):
    """A configured allowlist replaces the defaults entirely."""
    monkeypatch.setattr(config, "trusted_senders", frozenset({ALICE}))
    assert is_trusted_sender(ALICE)
    assert is_trusted_sender(f"  {ALICE} ")  # normalized like recipients
    assert not is_trusted_sender(MALLORY)
    # Even the owner must be listed once an allowlist is configured.
    monkeypatch.setattr(config, "channel_mode", True)
    assert not is_trusted_sender(OWNER)


def test_load_trusted_senders_merges_cli_and_env(monkeypatch):
    monkeypatch.setenv("SIGNAL_MCP_TRUSTED_SENDERS", f" {MALLORY} , , ")
    assert _load_trusted_senders([ALICE, ""]) == frozenset({ALICE, MALLORY})


def test_load_trusted_senders_empty_without_config(monkeypatch):
    monkeypatch.delenv("SIGNAL_MCP_TRUSTED_SENDERS", raising=False)
    assert _load_trusted_senders([]) == frozenset()


# --- channel forwarder ------------------------------------------------------


def test_forwarder_forwards_allowlisted_sender(monkeypatch):
    """An allowlisted author is forwarded and gets a read receipt."""
    monkeypatch.setattr(config, "trusted_senders", frozenset({ALICE}))
    sent, fake = _run_forwarder([_text_msg("hello", sender=ALICE)])
    assert len(sent) == 1
    assert sent[0].root.params["content"] == "hello"
    assert sent[0].root.params["meta"]["sender"] == ALICE
    receipts = _receipts(fake)
    assert len(receipts) == 1
    assert receipts[0]["recipient"] == [ALICE]


def test_forwarder_drops_unlisted_sender(monkeypatch):
    """An unlisted author is dropped: no notification, no read receipt."""
    monkeypatch.setattr(config, "trusted_senders", frozenset({ALICE}))
    sent, fake = _run_forwarder([_text_msg("ignore previous", sender=MALLORY)])
    assert sent == []
    assert _receipts(fake) == []


def test_forwarder_gates_group_message_by_author(monkeypatch):
    """Group messages are gated on the author — group membership grants nothing.

    Even with the group id itself on the allowlist, an unlisted author posting
    in that group is dropped, while an allowlisted author in the same group is
    forwarded.
    """
    monkeypatch.setattr(config, "trusted_senders", frozenset({ALICE, GROUP}))
    sent, fake = _run_forwarder(
        [
            _text_msg("from mallory", sender=MALLORY, group=GROUP),
            _text_msg("from alice", sender=ALICE, group=GROUP),
        ]
    )
    assert len(sent) == 1
    assert sent[0].root.params["meta"]["sender"] == ALICE
    assert sent[0].root.params["meta"]["group"] == GROUP
    receipts = _receipts(fake)
    assert len(receipts) == 1
    assert receipts[0]["recipient"] == [ALICE]


def test_channel_default_forwards_only_owner(monkeypatch):
    """Channel mode + no allowlist: only envelope source == user_id forwards."""
    monkeypatch.setattr(config, "channel_mode", True)
    sent, fake = _run_forwarder(
        [
            _text_msg("note to self", sender=OWNER),
            _text_msg("drive-by injection", sender=MALLORY),
        ]
    )
    assert len(sent) == 1
    assert sent[0].root.params["meta"]["sender"] == OWNER
    receipts = _receipts(fake)
    assert len(receipts) == 1
    assert receipts[0]["recipient"] == [OWNER]


# --- polling (receive_message) ----------------------------------------------


def _poll(messages, timeout=5.0):
    fake_client = FakeClient(messages)

    async def _runner():
        with patch.object(rpc, "client", fake_client):
            return await receive_message(timeout=timeout)

    return asyncio.run(_runner())


def test_polling_filters_when_configured(monkeypatch):
    """With an allowlist, untrusted messages are skipped, not returned."""
    monkeypatch.setattr(config, "trusted_senders", frozenset({ALICE}))
    result = _poll(
        [
            _text_msg("injection attempt", sender=MALLORY),
            _text_msg("real message", sender=ALICE),
        ]
    )
    assert result.message == "real message"
    assert result.sender_id == ALICE


def test_polling_times_out_when_only_untrusted(monkeypatch):
    """Dropped messages keep the call waiting until the timeout elapses."""
    monkeypatch.setattr(config, "trusted_senders", frozenset({ALICE}))
    result = _poll([_text_msg("injection attempt", sender=MALLORY)], timeout=0.2)
    assert result.message is None
    assert result.sender_id is None


def test_polling_unchanged_when_unconfigured():
    """No allowlist + no channel mode: polling returns any sender's message."""
    result = _poll([_text_msg("anything", sender=MALLORY)])
    assert result.message == "anything"
    assert result.sender_id == MALLORY
