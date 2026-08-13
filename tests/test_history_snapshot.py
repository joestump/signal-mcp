"""Tests for snapshot, conversations, and started_at (#58)."""

from datetime import datetime, timezone
from unittest.mock import patch

from signal_mcp.config import config
from signal_mcp.history import (
    BufferedAttachment,
    BufferedMessage,
    ConversationBuffer,
    started_at,
)

ACCOUNT = "+15551234567"
OTHER_A = "+11111111111"
OTHER_B = "+12222222222"
GROUP_ID = "dGVhbQ=="


def _msg(
    key: str = OTHER_A,
    sender: str = OTHER_A,
    name: str | None = "Alice",
    text: str | None = "hello",
    timestamp: int = 1000,
    direction: str = "inbound",
    attachments: list[BufferedAttachment] | None = None,
) -> BufferedMessage:
    return BufferedMessage(
        conversation_key=key,
        direction=direction,  # type: ignore[arg-type]
        sender_id=sender,
        sender_name=name,
        text=text,
        timestamp=timestamp,
        attachments=attachments or [],
    )


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_snapshot_unknown_key_returns_empty_list():
    """snapshot on an unknown key returns [], not KeyError."""
    buf = ConversationBuffer()
    assert buf.snapshot("+999") == []


def test_snapshot_is_point_in_time_copy():
    """A snapshot does not change when the buffer is later appended to."""
    buf = ConversationBuffer()
    buf.record(_msg(text="first", timestamp=1))
    snap = buf.snapshot(OTHER_A)
    assert len(snap) == 1
    # Append more — the snapshot must not change.
    buf.record(_msg(text="second", timestamp=2))
    assert len(snap) == 1
    assert len(buf.snapshot(OTHER_A)) == 2


def test_snapshot_oldest_first():
    """Snapshot returns messages oldest-first."""
    buf = ConversationBuffer()
    buf.record(_msg(text="a", timestamp=1))
    buf.record(_msg(text="b", timestamp=2))
    buf.record(_msg(text="c", timestamp=3))
    snap = buf.snapshot(OTHER_A)
    assert [m.text for m in snap] == ["a", "b", "c"]


def test_snapshot_contains_no_await():
    """snapshot is synchronous — no 'await' in its source."""
    import inspect

    from signal_mcp.history import ConversationBuffer

    source = inspect.getsource(ConversationBuffer.snapshot)
    assert "await" not in source


# ---------------------------------------------------------------------------
# conversations
# ---------------------------------------------------------------------------


def test_conversations_empty_buffer():
    """An empty buffer returns an empty list."""
    buf = ConversationBuffer()
    assert buf.conversations() == []


def test_conversations_most_recently_active_first():
    """Conversations are ordered most-recently-active first."""
    buf = ConversationBuffer()
    with patch.object(config, "history_conversation_cap", 10):
        buf.record(_msg(key="+111", text="a", timestamp=1))
        buf.record(_msg(key="+222", text="b", timestamp=2))
        buf.record(_msg(key="+333", text="c", timestamp=3))
    convos = buf.conversations()
    assert len(convos) == 3
    assert convos[0].key == "+333"  # most recent
    assert convos[1].key == "+222"
    assert convos[2].key == "+111"  # least recent


def test_conversations_summary_fields():
    """Each summary has key, label, preview, count, and last_activity."""
    buf = ConversationBuffer()
    buf.record(
        _msg(
            key=OTHER_A,
            sender=OTHER_A,
            name="Alice",
            text="hello world",
            timestamp=1000,
        )
    )
    buf.record(
        _msg(
            key=OTHER_A,
            sender=OTHER_A,
            name="Alice",
            text="second message",
            timestamp=2000,
        )
    )
    convos = buf.conversations()
    assert len(convos) == 1
    s = convos[0]
    assert s.key == OTHER_A
    assert s.label == "Alice"
    assert s.preview == "second message"
    assert s.message_count == 2
    assert s.last_activity == 2000


def test_conversations_preview_clipped():
    """Preview is clipped to ~80 chars."""
    buf = ConversationBuffer()
    long_text = "x" * 200
    buf.record(_msg(text=long_text, timestamp=1))
    convos = buf.conversations()
    assert len(convos[0].preview) <= 80
    assert convos[0].preview.endswith("...")


def test_conversations_preview_multiline_collapsed():
    """Preview collapses newlines to spaces."""
    buf = ConversationBuffer()
    buf.record(_msg(text="line one\nline two", timestamp=1))
    convos = buf.conversations()
    assert "\n" not in convos[0].preview


def test_conversations_attachment_only_preview():
    """An attachment-only message previews as its attachment description."""
    buf = ConversationBuffer()
    buf.record(
        _msg(
            text=None,
            timestamp=1,
            attachments=[
                BufferedAttachment(
                    id="file.png",
                    filename="photo.png",
                    content_type="image/png",
                    size=12345,
                )
            ],
        )
    )
    convos = buf.conversations()
    assert convos[0].preview == "photo.png"


# ---------------------------------------------------------------------------
# started_at
# ---------------------------------------------------------------------------


def test_started_at_returns_datetime():
    """started_at() returns a timezone-aware datetime."""
    ts = started_at()
    assert isinstance(ts, datetime)
    assert ts.tzinfo is not None


def test_started_at_is_consistent():
    """started_at() returns the same value on each call (module-level constant)."""
    t1 = started_at()
    t2 = started_at()
    assert t1 == t2


def test_started_at_is_recent():
    """started_at is close to now (within a few seconds)."""
    ts = started_at()
    now = datetime.now(timezone.utc)
    delta = now - ts
    assert delta.total_seconds() < 60  # within a minute


def test_group_conversation_label_stays_the_group_key():
    """A group thread is not renamed after whoever spoke last.

    The label used to become the newest inbound sender's name regardless of
    whether the conversation was a DM, so a group flipped between member names
    and was indistinguishable from a DM with the last speaker.
    """
    buf = ConversationBuffer()
    buf.record(
        _msg(key=GROUP_ID, sender=OTHER_A, name="Alice", text="hi", timestamp=1000)
    )
    assert buf.conversations()[0].label == GROUP_ID

    buf.record(
        _msg(key=GROUP_ID, sender=OTHER_B, name="Bob", text="hey", timestamp=2000)
    )
    convos = buf.conversations()
    assert len(convos) == 1
    assert convos[0].label == GROUP_ID
    assert convos[0].preview == "hey"


def test_dm_label_still_uses_sender_name():
    """In a DM the sender is the conversation, so the name is the right label."""
    buf = ConversationBuffer()
    buf.record(
        _msg(key=OTHER_A, sender=OTHER_A, name="Alice", text="hi", timestamp=1000)
    )
    assert buf.conversations()[0].label == "Alice"


def test_outbound_only_conversation_labels_with_key():
    """An outbound-only thread keeps the key as its label."""
    buf = ConversationBuffer()
    buf.record(
        _msg(
            key=OTHER_A,
            sender=ACCOUNT,
            name=None,
            text="sent",
            timestamp=1000,
            direction="outbound",
        )
    )
    assert buf.conversations()[0].label == OTHER_A
