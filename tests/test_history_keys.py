"""Tests for conversation keying and buffered message types (#55)."""

from signal_mcp.history import (
    BufferedAttachment,
    BufferedMessage,
    BufferedReaction,
    conversation_key,
)

ACCOUNT = "+15551234567"
OTHER = "+11234567890"
GROUP_ID = "aGVsbG8gd29ybGQ="


# ---------------------------------------------------------------------------
# conversation_key — five WHEN/THEN cases from SPEC-0001
# ---------------------------------------------------------------------------


def test_group_id_wins():
    """WHEN traffic carries a group_id THEN the key is that group id."""
    key = conversation_key(
        group_id=GROUP_ID,
        sender_id=OTHER,
        destination=None,
        account=ACCOUNT,
    )
    assert key == GROUP_ID


def test_inbound_dm_keyed_by_sender():
    """WHEN an inbound DM arrives from a counterparty THEN the key is the sender's number."""
    key = conversation_key(
        group_id=None,
        sender_id=OTHER,
        destination=None,
        account=ACCOUNT,
    )
    assert key == OTHER


def test_outbound_dm_keyed_by_destination():
    """WHEN an outbound/sync-sent DM is recorded THEN the key is the destination number."""
    key = conversation_key(
        group_id=None,
        sender_id=ACCOUNT,
        destination=OTHER,
        account=ACCOUNT,
    )
    assert key == OTHER


def test_note_to_self_keyed_by_account():
    """WHEN sender, destination, and account are all the same THEN the key is the account's number."""
    key = conversation_key(
        group_id=None,
        sender_id=ACCOUNT,
        destination=ACCOUNT,
        account=ACCOUNT,
    )
    assert key == ACCOUNT


def test_nothing_to_key_on_returns_none():
    """WHEN there is no group id, no sender, and no destination THEN the key is None."""
    key = conversation_key(
        group_id=None,
        sender_id=None,
        destination=None,
        account=ACCOUNT,
    )
    assert key is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_sync_sent_dm_destination_overrides_sender():
    """For sync-sent DMs, destination takes priority over sender."""
    key = conversation_key(
        group_id=None,
        sender_id=ACCOUNT,  # the account itself is the sender (sync-sent)
        destination=OTHER,
        account=ACCOUNT,
    )
    assert key == OTHER


def test_group_id_wins_even_with_destination():
    """Group id takes priority over everything."""
    key = conversation_key(
        group_id=GROUP_ID,
        sender_id=OTHER,
        destination=ACCOUNT,
        account=ACCOUNT,
    )
    assert key == GROUP_ID


# ---------------------------------------------------------------------------
# Dataclass smoke tests
# ---------------------------------------------------------------------------


def test_buffered_message_defaults():
    """BufferedMessage has sensible defaults for optional fields."""
    msg = BufferedMessage(
        conversation_key=OTHER,
        direction="inbound",
        sender_id=OTHER,
        sender_name="Bob",
        text="hello",
        timestamp=12345,
    )
    assert msg.attachments == []
    assert msg.reactions == []
    assert msg.truncated is False


def test_buffered_attachment_is_metadata_only():
    """BufferedAttachment has only metadata fields — no path, no url, no bytes."""
    att = BufferedAttachment(
        id="file.png",
        filename="photo.png",
        content_type="image/png",
        size=12345,
    )
    assert att.id == "file.png"
    assert att.filename == "photo.png"
    assert att.content_type == "image/png"
    assert att.size == 12345
    # Verify the dataclass has exactly these 4 fields.
    import dataclasses

    fields = {f.name for f in dataclasses.fields(BufferedAttachment)}
    assert fields == {"id", "filename", "content_type", "size"}


def test_buffered_reaction():
    reaction = BufferedReaction(
        emoji="\U0001f44d",
        author=OTHER,
        author_name="Bob",
    )
    assert reaction.emoji == "\U0001f44d"
    assert reaction.author == OTHER
    assert reaction.author_name == "Bob"
