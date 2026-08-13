"""Tests for reaction attachment in the conversation buffer (#57)."""

import logging
from unittest.mock import patch

from signal_mcp.config import config
from signal_mcp.history import (
    BufferedMessage,
    ConversationBuffer,
)

ACCOUNT = "+15551234567"
OTHER_A = "+11111111111"
OTHER_B = "+12222222222"


def _msg(
    key: str = OTHER_A,
    sender: str = OTHER_A,
    name: str = "Alice",
    text: str = "hello",
    timestamp: int = 1000,
) -> BufferedMessage:
    return BufferedMessage(
        conversation_key=key,
        direction="inbound",
        sender_id=sender,
        sender_name=name,
        text=text,
        timestamp=timestamp,
    )


def test_reaction_attaches_to_target_message():
    """A reaction referencing a buffered message's author+timestamp attaches to it."""
    buf = ConversationBuffer()
    buf.record(_msg(sender=OTHER_A, timestamp=1000))
    buf.record_reaction(
        conversation_key=OTHER_A,
        emoji="\U0001f44d",
        author=OTHER_B,
        author_name="Bob",
        target_author=OTHER_A,
        target_timestamp=1000,
        is_remove=False,
        trusted_check=False,
    )
    snap = buf.snapshot(OTHER_A)
    assert len(snap[0].reactions) == 1
    assert snap[0].reactions[0].emoji == "\U0001f44d"
    assert snap[0].reactions[0].author == OTHER_B


def test_reaction_replace_by_author():
    """A second reaction from the same author replaces the first."""
    buf = ConversationBuffer()
    buf.record(_msg(timestamp=1000))
    buf.record_reaction(
        conversation_key=OTHER_A,
        emoji="\U0001f44d",
        author=OTHER_B,
        author_name="Bob",
        target_author=OTHER_A,
        target_timestamp=1000,
        is_remove=False,
        trusted_check=False,
    )
    buf.record_reaction(
        conversation_key=OTHER_A,
        emoji="\u2764\ufe0f",
        author=OTHER_B,
        author_name="Bob",
        target_author=OTHER_A,
        target_timestamp=1000,
        is_remove=False,
        trusted_check=False,
    )
    snap = buf.snapshot(OTHER_A)
    assert len(snap[0].reactions) == 1
    assert snap[0].reactions[0].emoji == "\u2764\ufe0f"


def test_multiple_authors_reactions_persist():
    """Two different authors reacting to the same message both persist."""
    buf = ConversationBuffer()
    buf.record(_msg(timestamp=1000))
    buf.record_reaction(
        conversation_key=OTHER_A,
        emoji="\U0001f44d",
        author=OTHER_B,
        author_name="Bob",
        target_author=OTHER_A,
        target_timestamp=1000,
        is_remove=False,
        trusted_check=False,
    )
    buf.record_reaction(
        conversation_key=OTHER_A,
        emoji="\u2764\ufe0f",
        author=ACCOUNT,
        author_name="Me",
        target_author=OTHER_A,
        target_timestamp=1000,
        is_remove=False,
        trusted_check=False,
    )
    snap = buf.snapshot(OTHER_A)
    assert len(snap[0].reactions) == 2


def test_reaction_remove():
    """An is_remove reaction clears the author's existing reaction."""
    buf = ConversationBuffer()
    buf.record(_msg(timestamp=1000))
    buf.record_reaction(
        conversation_key=OTHER_A,
        emoji="\U0001f44d",
        author=OTHER_B,
        author_name="Bob",
        target_author=OTHER_A,
        target_timestamp=1000,
        is_remove=False,
        trusted_check=False,
    )
    buf.record_reaction(
        conversation_key=OTHER_A,
        emoji="\U0001f44d",
        author=OTHER_B,
        author_name=None,
        target_author=OTHER_A,
        target_timestamp=1000,
        is_remove=True,
        trusted_check=False,
    )
    snap = buf.snapshot(OTHER_A)
    assert len(snap[0].reactions) == 0


def test_reaction_orphan_ignored():
    """A reaction targeting a message not in the buffer is a silent no-op."""
    buf = ConversationBuffer()
    buf.record(_msg(timestamp=1000))
    buf.record_reaction(
        conversation_key=OTHER_A,
        emoji="\U0001f44d",
        author=OTHER_B,
        author_name="Bob",
        target_author=OTHER_A,
        target_timestamp=9999,  # non-existent
        is_remove=False,
        trusted_check=False,
    )
    snap = buf.snapshot(OTHER_A)
    assert len(snap[0].reactions) == 0


def test_reaction_to_evicted_message_harmless():
    """Reacting to a message evicted by the FIFO cap is harmless."""
    buf = ConversationBuffer()
    with patch.object(config, "history_message_cap", 2):
        buf.record(_msg(timestamp=1000))  # will be evicted
        buf.record(_msg(timestamp=2000))
        buf.record(_msg(timestamp=3000))  # evicts timestamp=1000
    # Now react to the evicted message — should be a no-op.
    buf.record_reaction(
        conversation_key=OTHER_A,
        emoji="\U0001f44d",
        author=OTHER_B,
        author_name="Bob",
        target_author=OTHER_A,
        target_timestamp=1000,
        is_remove=False,
        trusted_check=False,
    )
    snap = buf.snapshot(OTHER_A)
    for msg in snap:
        assert len(msg.reactions) == 0


def test_reaction_trust_gated():
    """Untrusted authors' reactions are dropped."""
    buf = ConversationBuffer()
    buf.record(_msg(timestamp=1000))
    with patch.object(config, "trusted_senders", frozenset({OTHER_A})):
        buf.record_reaction(
            conversation_key=OTHER_A,
            emoji="\U0001f44d",
            author=OTHER_B,
            author_name="Bob",
            target_author=OTHER_A,
            target_timestamp=1000,
            is_remove=False,
        )
    snap = buf.snapshot(OTHER_A)
    assert len(snap[0].reactions) == 0


def test_reaction_never_raises(caplog):
    """An internal failure is caught and logged, not propagated."""
    buf = ConversationBuffer()
    buf.record(_msg(timestamp=1000))

    # Force an internal error by replacing the conversations dict with an
    # object whose .get() raises.
    class ExplodingDict:
        def get(self, key, default=None):
            raise RuntimeError("boom")

        def __contains__(self, key):
            return True

    with patch.object(buf, "_conversations", ExplodingDict()):
        with caplog.at_level(logging.WARNING):
            buf.record_reaction(
                conversation_key=OTHER_A,
                emoji="\U0001f44d",
                author=OTHER_B,
                author_name="Bob",
                target_author=OTHER_A,
                target_timestamp=1000,
                is_remove=False,
                trusted_check=False,
            )
    assert any("failed to record reaction" in r.message for r in caplog.records)
