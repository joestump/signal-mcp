"""Tests for ConversationBuffer bounds, eviction, truncation, and trust gating (#56)."""

from unittest.mock import patch
import logging


from signal_mcp.config import config
from signal_mcp.history import (
    BufferedAttachment,
    BufferedMessage,
    ConversationBuffer,
    TRUNCATION_MARKER,
    _truncate_text,
    _clamp_metadata,
    record_inbound,
    record_outbound,
)
from signal_mcp.parse import Attachment, MessageResponse

ACCOUNT = "+15551234567"
OTHER = "+11234567890"
GROUP_ID = "dGVhbQ=="


def _msg(
    key: str = OTHER,
    text: str = "hello",
    timestamp: int = 1000,
    direction: str = "inbound",
    attachments: list[BufferedAttachment] | None = None,
) -> BufferedMessage:
    return BufferedMessage(
        conversation_key=key,
        direction=direction,  # type: ignore[arg-type]
        sender_id=OTHER if direction == "inbound" else ACCOUNT,
        sender_name="Bob",
        text=text,
        timestamp=timestamp,
        attachments=attachments or [],
    )


# ---------------------------------------------------------------------------
# _truncate_text
# ---------------------------------------------------------------------------


def test_truncate_text_none():
    assert _truncate_text(None, 100) == (None, False)


def test_truncate_text_short():
    assert _truncate_text("hi", 100) == ("hi", False)


def test_truncate_text_exact():
    assert _truncate_text("hi", 2) == ("hi", False)


def test_truncate_text_truncates_and_marks():
    result, truncated = _truncate_text("hello world", 5)
    assert truncated is True
    assert result is not None
    assert result.endswith(TRUNCATION_MARKER)
    # The visible text (minus marker) is at most 5 bytes.
    visible = result[: -len(TRUNCATION_MARKER)]
    assert len(visible.encode("utf-8")) <= 5


def test_truncate_text_multibyte_no_split():
    """A multi-byte character at the boundary must not be split."""
    # Each emoji is 4 bytes in UTF-8.
    text = "\U0001f600" * 10  # 40 bytes
    result, truncated = _truncate_text(text, 6)  # allow 1 full emoji (4 bytes)
    assert truncated is True
    assert result is not None
    # Should decode without error (no UnicodeDecodeError raised).
    assert isinstance(result, str)
    assert result.endswith(TRUNCATION_MARKER)
    # The visible text should be at most 1 emoji (4 bytes, since 6 // 4 = 1).
    visible = result[: -len(TRUNCATION_MARKER)]
    assert len(visible.encode("utf-8")) <= 6


# ---------------------------------------------------------------------------
# _clamp_metadata
# ---------------------------------------------------------------------------


def test_clamp_metadata_none():
    assert _clamp_metadata(None) is None


def test_clamp_metadata_short():
    assert _clamp_metadata("photo.png") == "photo.png"


def test_clamp_metadata_long():
    long_name = "x" * 300
    clamped = _clamp_metadata(long_name)
    assert clamped is not None
    assert len(clamped.encode("utf-8")) <= 256


# ---------------------------------------------------------------------------
# ConversationBuffer — FIFO per-conversation eviction
# ---------------------------------------------------------------------------


def test_fifo_eviction_within_conversation():
    """Messages beyond the per-conversation cap evict oldest first."""
    buf = ConversationBuffer()
    with patch.object(config, "history_message_cap", 3):
        for i in range(5):
            buf.record(_msg(text=f"msg-{i}", timestamp=i))
    snapshot = buf.snapshot(OTHER)
    assert len(snapshot) == 3
    assert snapshot[0].text == "msg-2"
    assert snapshot[2].text == "msg-4"


# ---------------------------------------------------------------------------
# ConversationBuffer — LRU cross-conversation eviction
# ---------------------------------------------------------------------------


def test_lru_eviction_across_conversations():
    """Conversations beyond the total cap evict least-recently-active."""
    buf = ConversationBuffer()
    with (
        patch.object(config, "history_message_cap", 10),
        patch.object(config, "history_conversation_cap", 3),
    ):
        buf.record(_msg(key="+111", text="a", timestamp=1))
        buf.record(_msg(key="+222", text="b", timestamp=2))
        buf.record(_msg(key="+333", text="c", timestamp=3))
        # +111 is least-recently-active; new conv should evict it.
        buf.record(_msg(key="+444", text="d", timestamp=4))

    assert buf.snapshot("+111") == []
    assert len(buf.snapshot("+222")) == 1
    assert len(buf.snapshot("+333")) == 1
    assert len(buf.snapshot("+444")) == 1


def test_lru_move_to_end_keeps_active_conversation():
    """Recording into an existing conversation moves it to most-recently-active."""
    buf = ConversationBuffer()
    with (
        patch.object(config, "history_message_cap", 10),
        patch.object(config, "history_conversation_cap", 2),
    ):
        buf.record(_msg(key="+111", text="a", timestamp=1))
        buf.record(_msg(key="+222", text="b", timestamp=2))
        # Touch +111 again so it becomes most-recently-active.
        buf.record(_msg(key="+111", text="c", timestamp=3))
        # New conversation should evict +222 (LRU), not +111.
        buf.record(_msg(key="+333", text="d", timestamp=4))

    assert len(buf.snapshot("+111")) == 2
    assert buf.snapshot("+222") == []
    assert len(buf.snapshot("+333")) == 1


# ---------------------------------------------------------------------------
# ConversationBuffer — text truncation
# ---------------------------------------------------------------------------


def test_text_truncation_at_record_time():
    """Long text is truncated with a marker at record time."""
    buf = ConversationBuffer()
    with patch.object(config, "history_text_cap", 10):
        buf.record(_msg(text="x" * 100, timestamp=1))
    snapshot = buf.snapshot(OTHER)
    assert len(snapshot) == 1
    assert snapshot[0].truncated is True
    msg_text = snapshot[0].text
    assert msg_text is not None
    assert msg_text.endswith(TRUNCATION_MARKER)


# ---------------------------------------------------------------------------
# ConversationBuffer — attachment metadata clamping
# ---------------------------------------------------------------------------


def test_attachment_metadata_clamped():
    """Sender-controlled attachment strings are clamped at record time."""
    buf = ConversationBuffer()
    long_filename = "x" * 300
    long_content_type = "y" * 300
    buf.record(
        _msg(
            text="img",
            attachments=[
                BufferedAttachment(
                    id="file.png",
                    filename=long_filename,
                    content_type=long_content_type,
                    size=12345,
                )
            ],
        )
    )
    snapshot = buf.snapshot(OTHER)
    assert len(snapshot) == 1
    att = snapshot[0].attachments[0]
    assert len(att.filename.encode("utf-8")) <= 256  # type: ignore[union-attr]
    assert len(att.content_type.encode("utf-8")) <= 256  # type: ignore[union-attr]
    assert att.size == 12345
    assert att.id == "file.png"


# ---------------------------------------------------------------------------
# ConversationBuffer — never raises
# ---------------------------------------------------------------------------


def test_record_never_raises(caplog):
    """An internal failure is caught and logged, not propagated."""
    buf = ConversationBuffer()
    # Force an internal error by monkeypatching.
    with patch.object(config, "history_message_cap", side_effect=RuntimeError("boom")):
        with caplog.at_level(logging.WARNING):
            buf.record(_msg())
    # The call returned normally; the warning was logged.
    assert any("failed to record" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# record_inbound — trust gating
# ---------------------------------------------------------------------------


def test_record_inbound_drops_untrusted_sender():
    """When trusted_senders is configured, untrusted authors are not recorded."""
    buf2 = ConversationBuffer()
    with (
        patch.object(config, "trusted_senders", frozenset({ACCOUNT})),
        patch.object(config, "account", ACCOUNT),
        patch("signal_mcp.history.buffer", buf2),
    ):
        record_inbound(
            MessageResponse(
                message="spam",
                sender_id=OTHER,
                sender_name="Stranger",
                timestamp=1000,
            )
        )
    assert buf2.snapshot(OTHER) == []


def test_record_inbound_records_trusted_sender():
    """Trusted senders are recorded."""
    buf2 = ConversationBuffer()
    with (
        patch.object(config, "trusted_senders", frozenset({OTHER})),
        patch.object(config, "account", ACCOUNT),
        patch("signal_mcp.history.buffer", buf2),
    ):
        record_inbound(
            MessageResponse(
                message="hello",
                sender_id=OTHER,
                sender_name="Bob",
                timestamp=1000,
            )
        )
    snapshot = buf2.snapshot(OTHER)
    assert len(snapshot) == 1
    assert snapshot[0].text == "hello"


def test_record_inbound_records_attachments_metadata_only():
    """Attachments are stored as metadata only — no path, no url."""
    buf2 = ConversationBuffer()
    with (
        patch.object(config, "trusted_senders", frozenset()),
        patch.object(config, "account", ACCOUNT),
        patch("signal_mcp.history.buffer", buf2),
    ):
        record_inbound(
            MessageResponse(
                message="see this",
                sender_id=OTHER,
                timestamp=1000,
                attachments=[
                    Attachment(
                        id="file.png",
                        content_type="image/png",
                        filename="photo.png",
                        size=12345,
                        path="/some/path",
                        url="https://presigned.example.com/file",
                    )
                ],
            )
        )
    snapshot = buf2.snapshot(OTHER)
    assert len(snapshot) == 1
    att = snapshot[0].attachments[0]
    assert att.id == "file.png"
    assert att.size == 12345


# ---------------------------------------------------------------------------
# record_outbound
# ---------------------------------------------------------------------------


def test_record_outbound_dm():
    """Outbound DM is recorded under the destination's key."""
    buf2 = ConversationBuffer()
    with (
        patch.object(config, "account", ACCOUNT),
        patch("signal_mcp.history.buffer", buf2),
    ):
        record_outbound(text="reply", timestamp=2000, target=OTHER, is_group=False)
    snapshot = buf2.snapshot(OTHER)
    assert len(snapshot) == 1
    assert snapshot[0].direction == "outbound"
    assert snapshot[0].sender_id == ACCOUNT
    assert snapshot[0].text == "reply"


def test_record_outbound_group():
    """Outbound group message is recorded under the group id."""
    buf2 = ConversationBuffer()
    with (
        patch.object(config, "account", ACCOUNT),
        patch("signal_mcp.history.buffer", buf2),
    ):
        record_outbound(
            text="team update", timestamp=3000, target=GROUP_ID, is_group=True
        )
    snapshot = buf2.snapshot(GROUP_ID)
    assert len(snapshot) == 1
    assert snapshot[0].direction == "outbound"


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_snapshot_unknown_key_returns_empty():
    buf = ConversationBuffer()
    assert buf.snapshot("+999") == []


def test_snapshot_is_a_copy():
    """Snapshot returns a list copy so caller is immune to mutation."""
    buf = ConversationBuffer()
    buf.record(_msg(text="hello", timestamp=1))
    snap = buf.snapshot(OTHER)
    assert len(snap) == 1
    # Mutating the snapshot should not affect the buffer.
    snap.clear()
    assert len(buf.snapshot(OTHER)) == 1
