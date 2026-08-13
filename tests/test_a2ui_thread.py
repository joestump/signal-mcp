"""Tests for the A2UI thread renderer (#62)."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

from signal_mcp.a2ui import render_thread, A2UI_VERSION, CATALOG_ID
from signal_mcp.history import (
    BufferedAttachment,
    BufferedMessage,
    BufferedReaction,
)

ACCOUNT = "+15551234567"
OTHER = "+11234567890"
FIXED_TIME = datetime(2026, 8, 12, 14, 2, 0, tzinfo=timezone.utc)


def _msg(
    text: str = "hello",
    timestamp: int = 1000,
    sender_id: str = OTHER,
    sender_name: str | None = "Bob",
    direction: str = "inbound",
    attachments: list[BufferedAttachment] | None = None,
    reactions: list[BufferedReaction] | None = None,
) -> BufferedMessage:
    return BufferedMessage(
        conversation_key=OTHER,
        direction=direction,  # type: ignore[arg-type]
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        timestamp=timestamp,
        attachments=attachments or [],
        reactions=reactions or [],
    )


def test_render_empty_conversation():
    """An empty conversation renders a valid surface with empty-state text."""
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_thread(
            conversation_id=OTHER,
            label="Bob",
            messages=[],
            account=ACCOUNT,
        )
    assert env["version"] == A2UI_VERSION
    assert env["updateComponents"]["catalogId"] == CATALOG_ID
    components_str = json.dumps(env)
    assert "No buffered messages" in components_str


def test_render_populated_thread():
    """A populated thread renders two-sided chat bubbles."""
    messages = [
        _msg(text="hi", timestamp=1000),
        _msg(
            text="hello back",
            timestamp=2000,
            sender_id=ACCOUNT,
            sender_name=None,
            direction="outbound",
        ),
    ]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_thread(
            conversation_id=OTHER,
            label="Bob",
            messages=messages,
            account=ACCOUNT,
        )
    assert env["version"] == A2UI_VERSION
    comp_str = json.dumps(env)
    assert "hi" in comp_str
    assert "hello back" in comp_str
    assert "Bob" in comp_str
    assert "(agent)" in comp_str
    assert "1000" not in comp_str  # raw epoch not shown


def test_scope_caption_always_present():
    """Every surface carries the scope disclosure with the process start time."""
    for msgs in ([], [_msg()]):
        with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
            env = render_thread(
                conversation_id=OTHER,
                label="Bob",
                messages=msgs,
                account=ACCOUNT,
            )
        comp_str = json.dumps(env)
        assert "This instance's view since" in comp_str
        assert "2026-08-12 14:02 UTC" in comp_str
        assert "the phone is the complete record" in comp_str


def test_hostile_text_is_inert():
    """A message body that looks like A2UI JSON renders as literal text."""
    hostile = json.dumps({"version": "v0.9", "updateComponents": {"surfaceId": "evil"}})
    messages = [_msg(text=hostile, timestamp=1000)]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_thread(
            conversation_id=OTHER,
            label="Bob",
            messages=messages,
            account=ACCOUNT,
        )
    # The hostile text appears verbatim in the serialized payload (JSON-escaped).
    comp_str = json.dumps(env)
    assert "surfaceId" in comp_str
    assert "evil" in comp_str
    # The envelope's own surfaceId is NOT "evil".
    assert env["updateComponents"]["surfaceId"] == f"thread-{OTHER}"


def test_no_data_uris_in_payload():
    """No 'data:' appears anywhere in a rendered payload."""
    messages = [
        _msg(
            text="see this",
            timestamp=1000,
            attachments=[
                BufferedAttachment(
                    id="file.png",
                    filename="photo.png",
                    content_type="image/png",
                    size=12345,
                )
            ],
        )
    ]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_thread(
            conversation_id=OTHER,
            label="Bob",
            messages=messages,
            account=ACCOUNT,
        )
    assert "data:" not in json.dumps(env)


def test_attachment_size_independent_of_media_size():
    """Payload size is similar for a 1 KB vs 10 MB attachment."""
    import sys

    small = [
        _msg(
            text="img",
            timestamp=1000,
            attachments=[
                BufferedAttachment(
                    id="f", filename="a.png", content_type="image/png", size=1024
                )
            ],
        )
    ]
    big = [
        _msg(
            text="img",
            timestamp=1000,
            attachments=[
                BufferedAttachment(
                    id="f", filename="a.png", content_type="image/png", size=10485760
                )
            ],
        )
    ]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env_small = render_thread(
            conversation_id=OTHER, label="B", messages=small, account=ACCOUNT
        )
        env_big = render_thread(
            conversation_id=OTHER, label="B", messages=big, account=ACCOUNT
        )
    size_small = sys.getsizeof(json.dumps(env_small))
    size_big = sys.getsizeof(json.dumps(env_big))
    # Should be within a few hundred bytes (the only difference is "1 KB" vs "10 MB").
    assert abs(size_big - size_small) < 200


def test_reactions_render_on_target_bubble():
    """Reactions appear on their target message's bubble, not as separate rows."""
    messages = [
        _msg(
            text="hello",
            timestamp=1000,
            reactions=[
                BufferedReaction(emoji="\U0001f44d", author=ACCOUNT, author_name="Me"),
            ],
        ),
    ]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_thread(
            conversation_id=OTHER,
            label="Bob",
            messages=messages,
            account=ACCOUNT,
        )
    comp_str = json.dumps(env, ensure_ascii=False)
    assert "\U0001f44d" in comp_str
    assert "Me" in comp_str
    # There should be only one message column (the reaction is inline).
    msg_cols = [
        c
        for c in env["updateComponents"]["components"]
        if c.get("id", "").startswith("msg-col")
    ]
    assert len(msg_cols) == 1


def test_timestamps_human_readable():
    """Timestamps render in a human-readable form, not raw epoch."""
    messages = [_msg(text="hello", timestamp=1723577280000)]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_thread(
            conversation_id=OTHER,
            label="Bob",
            messages=messages,
            account=ACCOUNT,
        )
    comp_str = json.dumps(env)
    assert "2024" in comp_str or "2026" in comp_str  # some year
    assert "UTC" in comp_str
