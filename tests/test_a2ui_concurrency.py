"""Concurrency safety tests — snapshot rendering during arrival (#68)."""

import asyncio
from unittest.mock import patch

from signal_mcp.a2ui import validate_adjacency
from signal_mcp.config import config
from signal_mcp.history import BufferedMessage, ConversationBuffer

ACCOUNT = "+15551234567"
OTHER = "+11234567890"


def _msg(text: str, timestamp: int) -> BufferedMessage:
    return BufferedMessage(
        conversation_key=OTHER,
        direction="inbound",
        sender_id=OTHER,
        sender_name="Bob",
        text=text,
        timestamp=timestamp,
    )


def test_concurrent_arrival_and_render():
    """Interleave thread renders with new messages arriving — every render is valid."""

    async def scenario():
        buf = ConversationBuffer()
        with (
            patch.object(config, "account", ACCOUNT),
            patch("signal_mcp.history.buffer", buf),
            patch.object(config, "history_message_cap", 200),
            patch.object(config, "history_conversation_cap", 50),
            patch.object(config, "history_text_cap", 4096),
        ):
            from signal_mcp.a2ui import render_thread

            for i in range(50):
                buf.record(_msg(f"msg-{i}", 1000 + i))
                # Render mid-arrival.
                messages = buf.snapshot(OTHER)
                env = render_thread(
                    conversation_id=OTHER,
                    label="Bob",
                    messages=messages,
                    account=ACCOUNT,
                )
                # Every render must produce a valid envelope.
                components = env["updateComponents"]["components"]
                validate_adjacency(components)
                # The rendered count must never exceed the buffer's.
                assert len(messages) == min(i + 1, 200)

    asyncio.run(scenario())


def test_snapshot_unchanged_after_arrival():
    """A snapshot taken before an arrival is unchanged after the arrival."""
    buf = ConversationBuffer()
    buf.record(_msg("first", 1000))
    snap = buf.snapshot(OTHER)
    assert len(snap) == 1

    # Arrival after snapshot.
    buf.record(_msg("second", 2000))
    assert len(snap) == 1  # snapshot unchanged
    assert len(buf.snapshot(OTHER)) == 2


def test_no_new_background_tasks():
    """Recording and rendering do not create new asyncio tasks."""

    async def scenario():
        buf = ConversationBuffer()
        with (
            patch.object(config, "account", ACCOUNT),
            patch("signal_mcp.history.buffer", buf),
            patch.object(config, "history_message_cap", 200),
            patch.object(config, "history_conversation_cap", 50),
            patch.object(config, "history_text_cap", 4096),
        ):
            from signal_mcp.a2ui import render_thread

            initial_tasks = len(asyncio.all_tasks())
            buf.record(_msg("hello", 1000))
            messages = buf.snapshot(OTHER)
            render_thread(
                conversation_id=OTHER,
                label="Bob",
                messages=messages,
                account=ACCOUNT,
            )
            final_tasks = len(asyncio.all_tasks())
            assert initial_tasks == final_tasks

    asyncio.run(scenario())
