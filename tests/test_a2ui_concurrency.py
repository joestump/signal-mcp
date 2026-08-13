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
    """Render on one task while another records, and prove the view holds still.

    The producer and the renderer are real asyncio tasks with yield points
    between every step, so the producer genuinely runs while the renderer is
    suspended part-way through a render. That is the only arrangement in which
    the snapshot guarantee can actually be observed: a sequential loop cannot
    interleave, so it would pass even if snapshot() handed back the live deque.
    """

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

            done = asyncio.Event()
            renders = 0

            async def produce() -> None:
                for i in range(50):
                    buf.record(_msg(f"msg-{i}", 1000 + i))
                    await asyncio.sleep(0)
                done.set()

            async def render() -> None:
                nonlocal renders
                while not done.is_set():
                    messages = buf.snapshot(OTHER)
                    observed = len(messages)
                    # Suspend mid-render: the producer records while we wait.
                    await asyncio.sleep(0)
                    # The snapshot must not have moved under us. Were snapshot()
                    # returning the live deque, this is where it would grow.
                    assert len(messages) == observed
                    env = render_thread(
                        conversation_id=OTHER,
                        label="Bob",
                        messages=messages,
                        account=ACCOUNT,
                    )
                    validate_adjacency(env["updateComponents"]["components"])
                    # The envelope must describe the snapshot it was handed,
                    # not whatever the buffer holds now.
                    body_ids = [
                        c["id"]
                        for c in env["updateComponents"]["components"]
                        if c["id"].startswith("msg-body-")
                    ]
                    assert len(body_ids) == observed
                    renders += 1
                    await asyncio.sleep(0)

            await asyncio.gather(produce(), render())

            # The renderer really did run alongside the producer, and really
            # did observe the buffer part-way through filling.
            assert renders > 1
            assert len(buf.snapshot(OTHER)) == 50

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
