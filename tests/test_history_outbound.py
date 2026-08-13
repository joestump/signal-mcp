"""Tests for outbound history buffer recording in tools.py (#60)."""

import asyncio
import contextlib
import logging
from collections.abc import Iterator
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


@contextlib.contextmanager
def _patched_buffer(buf: ConversationBuffer) -> Iterator[None]:
    """Patch every binding of the module-level buffer singleton.

    tools.py holds its own ``history_buffer`` alias for the reaction path,
    while message sends go through ``history.record_outbound``, which closes
    over ``history.buffer``. Patching only one leaves the other writing to the
    real singleton.
    """
    with (
        patch("signal_mcp.history.buffer", buf),
        patch("signal_mcp.tools.history_buffer", buf),
    ):
        yield


def test_outbound_dm_recorded():
    """A successful DM send is recorded in the buffer with the daemon-returned timestamp."""
    fake = FakeClient()
    buf = ConversationBuffer()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "account", ACCOUNT),
        _patched_buffer(buf),
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
        _patched_buffer(buf),
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
        _patched_buffer(buf),
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
        _patched_buffer(buf),
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
            "signal_mcp.history.conversation_key",
            side_effect=RuntimeError("buffer boom"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        # Should not raise — the send itself succeeds.
        asyncio.run(_send_message("reply", OTHER))

    assert any("record_outbound failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Attachment metadata — an attachment-only send must not buffer as blank
# ---------------------------------------------------------------------------


def test_outbound_local_path_attachment_recorded(tmp_path):
    """A local-path attachment is buffered as name/type/size metadata."""
    photo = tmp_path / "photo.png"
    photo.write_bytes(b"\x89PNG" + b"0" * 100)

    fake = FakeClient()
    buf = ConversationBuffer()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "account", ACCOUNT),
        _patched_buffer(buf),
    ):
        asyncio.run(_send_message("", OTHER, attachments=[str(photo)]))

    snap = buf.snapshot(OTHER)
    assert len(snap) == 1
    assert len(snap[0].attachments) == 1
    att = snap[0].attachments[0]
    assert att.filename == "photo.png"
    assert att.content_type == "image/png"
    assert att.size == 104
    # Metadata only — no path, no bytes.
    assert not hasattr(att, "path")


def test_outbound_data_uri_attachment_recorded():
    """A data URI yields its declared mime type, filename, and decoded size."""
    fake = FakeClient()
    buf = ConversationBuffer()
    # 9 bytes -> 12 base64 chars, no padding.
    data_uri = "data:image/jpeg;filename=snap%20shot.jpg;base64,MTIzNDU2Nzg5"
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "account", ACCOUNT),
        _patched_buffer(buf),
    ):
        asyncio.run(_send_message("", OTHER, attachments=[data_uri]))

    att = buf.snapshot(OTHER)[0].attachments[0]
    assert att.filename == "snap shot.jpg"
    assert att.content_type == "image/jpeg"
    assert att.size == 9


def test_outbound_attachment_only_send_is_not_blank():
    """An attachment-only send renders as an attachment, not an empty bubble."""
    fake = FakeClient()
    buf = ConversationBuffer()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "account", ACCOUNT),
        _patched_buffer(buf),
    ):
        asyncio.run(
            _send_message("", OTHER, attachments=["data:image/png;base64,MTIz"])
        )

    msg = buf.snapshot(OTHER)[0]
    assert not msg.text
    # Without the attachment record this message would be indistinguishable
    # from a blank one.
    assert len(msg.attachments) == 1
    assert msg.attachments[0].content_type == "image/png"


def test_outbound_unreadable_attachment_still_recorded():
    """A path that does not exist still yields a record, with size unknown."""
    fake = FakeClient()
    buf = ConversationBuffer()
    with (
        patch.object(rpc, "client", fake),
        patch.object(config, "account", ACCOUNT),
        _patched_buffer(buf),
    ):
        asyncio.run(_send_message("", OTHER, attachments=["/nope/missing.pdf"]))

    att = buf.snapshot(OTHER)[0].attachments[0]
    assert att.filename == "missing.pdf"
    assert att.content_type == "application/pdf"
    assert att.size is None
