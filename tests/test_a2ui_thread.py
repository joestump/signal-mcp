"""Tests for the A2UI thread renderer (#62)."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

from signal_mcp.a2ui import (
    A2UI_VERSION,
    CATALOG_ID,
    render_thread,
    validate_adjacency,
)
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
    assert "1970-01-01 00:00 UTC" in comp_str  # formatted, not raw epoch


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


# ---------------------------------------------------------------------------
# started_at_dt override — the documented parameter must actually be used
# ---------------------------------------------------------------------------


def _scope_text(env: dict) -> str:
    components = env["updateComponents"]["components"]
    return next(c["text"] for c in components if c["id"] == "scope")


def test_started_at_override_is_used():
    """started_at_dt overrides the process start time in the scope caption.

    The parameter used to be assigned to a local and discarded, so the caption
    always reported the real process start time no matter what a caller passed.
    """
    override = datetime(1999, 12, 31, 23, 58, tzinfo=timezone.utc)
    env = render_thread(
        conversation_id="c1",
        label="Alice",
        messages=[_msg(text="hi")],
        account=ACCOUNT,
        started_at_dt=override,
    )
    assert "1999-12-31 23:58 UTC" in _scope_text(env)


def test_started_at_override_is_used_on_empty_surface():
    """The empty-state surface honours the override too."""
    override = datetime(1999, 12, 31, 23, 58, tzinfo=timezone.utc)
    env = render_thread(
        conversation_id="c1",
        label="Alice",
        messages=[],
        account=ACCOUNT,
        started_at_dt=override,
    )
    assert "1999-12-31 23:58 UTC" in _scope_text(env)


# ---------------------------------------------------------------------------
# Hostile field values degrade their own line, not the whole surface
# ---------------------------------------------------------------------------


def test_out_of_range_timestamp_does_not_break_the_surface():
    """A sender-supplied timestamp outside datetime's range renders blank."""
    env = render_thread(
        conversation_id="c1",
        label="Alice",
        messages=[_msg(text="hi", timestamp=10**20)],
        account=ACCOUNT,
        started_at_dt=FIXED_TIME,
    )
    components = env["updateComponents"]["components"]
    assert any(c["id"] == "msg-time-0" and c["text"] == "" for c in components)
    # The message body still rendered.
    assert any(c.get("text") == "hi" for c in components)


def test_non_numeric_timestamp_does_not_break_the_surface():
    """A non-numeric timestamp renders blank instead of raising TypeError."""
    msg = _msg(text="hi")
    msg.timestamp = "not-a-number"  # type: ignore[assignment]
    env = render_thread(
        conversation_id="c1",
        label="Alice",
        messages=[msg],
        account=ACCOUNT,
        started_at_dt=FIXED_TIME,
    )
    components = env["updateComponents"]["components"]
    assert any(c["id"] == "msg-time-0" and c["text"] == "" for c in components)


def test_non_numeric_attachment_size_renders_unknown():
    """A non-numeric attachments[].size degrades to 'unknown size'."""
    msg = _msg(text="")
    msg.attachments = [
        BufferedAttachment(
            id="a1",
            filename="photo.png",
            content_type="image/png",
            size="huge",  # type: ignore[arg-type]
        )
    ]
    env = render_thread(
        conversation_id="c1",
        label="Alice",
        messages=[msg],
        account=ACCOUNT,
        started_at_dt=FIXED_TIME,
    )
    components = env["updateComponents"]["components"]
    assert any(
        c.get("text") == "photo.png (image/png, unknown size)" for c in components
    )


# ---------------------------------------------------------------------------
# Action buttons (#64)
# ---------------------------------------------------------------------------

GROUP_ID = "aGVsbG8="


def test_react_button_carries_action_context():
    """The react button's context contains conversation_id, target_author, and target_timestamp."""
    messages = [_msg(text="hello", timestamp=1000, sender_id=OTHER)]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_thread(
            conversation_id=OTHER,
            label="Bob",
            messages=messages,
            account=ACCOUNT,
        )
    components = env["updateComponents"]["components"]
    react_buttons = [c for c in components if c.get("actionName") == "react"]
    assert len(react_buttons) == 1
    ctx = react_buttons[0]["context"]
    assert ctx["conversation_id"] == OTHER
    assert ctx["target_author"] == OTHER
    assert ctx["target_timestamp"] == 1000


def test_react_button_context_for_group():
    """The react button context for a group conversation carries the group id."""
    group_msg = BufferedMessage(
        conversation_key=GROUP_ID,
        direction="inbound",
        sender_id=OTHER,
        sender_name="Bob",
        text="group hi",
        timestamp=2000,
    )
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_thread(
            conversation_id=GROUP_ID,
            label="Team",
            messages=[group_msg],
            account=ACCOUNT,
        )
    components = env["updateComponents"]["components"]
    react_buttons = [c for c in components if c.get("actionName") == "react"]
    assert len(react_buttons) == 1
    ctx = react_buttons[0]["context"]
    assert ctx["conversation_id"] == GROUP_ID
    assert ctx["target_author"] == OTHER
    assert ctx["target_timestamp"] == 2000


def test_every_button_has_text_label():
    """Every Button in a rendered surface has a non-empty text label."""
    messages = [_msg(text="hello", timestamp=1000)]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_thread(
            conversation_id=OTHER,
            label="Bob",
            messages=messages,
            account=ACCOUNT,
        )
    components = env["updateComponents"]["components"]
    buttons = [c for c in components if c.get("component") == "Button"]
    assert len(buttons) >= 2  # reply + react
    for btn in buttons:
        label = btn.get("label", "")
        assert isinstance(label, str) and len(label) > 0, btn


def test_reply_button_exists():
    """A reply button is present on every message bubble."""
    messages = [_msg(text="hello", timestamp=1000)]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_thread(
            conversation_id=OTHER,
            label="Bob",
            messages=messages,
            account=ACCOUNT,
        )
    components = env["updateComponents"]["components"]
    reply_buttons = [c for c in components if c.get("actionName") == "reply"]
    assert len(reply_buttons) == 1


# ---------------------------------------------------------------------------
# Action button context (#64)
# ---------------------------------------------------------------------------


def _buttons(env: dict, action: str) -> list[dict]:
    return [
        c
        for c in env["updateComponents"]["components"]
        if c.get("actionName") == action
    ]


def test_reply_button_carries_conversation_context():
    """Reply must say which conversation it belongs to.

    It previously carried an action name and nothing else, leaving every
    Reply button in the surface byte-identical apart from its id — a phase-2
    handler had no way to route the reply.
    """
    env = render_thread(
        conversation_id=OTHER,
        label="Bob",
        messages=[_msg(text="hi"), _msg(text="there", timestamp=2000)],
        account=ACCOUNT,
        started_at_dt=FIXED_TIME,
    )
    replies = _buttons(env, "reply")
    assert len(replies) == 2
    for btn in replies:
        assert btn["context"]["conversation_id"] == OTHER


def test_react_button_omitted_without_a_usable_target():
    """No React button when the message has no (author, timestamp) to target.

    record_outbound stores timestamp=None whenever the daemon omits one, and
    a reaction against timestamp 0 / author "" matches nothing on the phone.
    """
    msg = _msg(text="sent")
    msg.timestamp = None
    env = render_thread(
        conversation_id=OTHER,
        label="Bob",
        messages=[msg],
        account=ACCOUNT,
        started_at_dt=FIXED_TIME,
    )
    assert _buttons(env, "react") == []
    # Reply is still offered — it needs no target.
    assert len(_buttons(env, "reply")) == 1
    # And the surface is still structurally valid with the button missing.
    validate_adjacency(env["updateComponents"]["components"])


def test_react_button_omitted_without_a_sender():
    """An author-less message cannot be reacted to either."""
    msg = _msg(text="sent")
    msg.sender_id = None
    env = render_thread(
        conversation_id=OTHER,
        label="Bob",
        messages=[msg],
        account=ACCOUNT,
        started_at_dt=FIXED_TIME,
    )
    assert _buttons(env, "react") == []
    validate_adjacency(env["updateComponents"]["components"])


def test_react_context_is_never_a_placeholder_target():
    """A rendered React button always names a real, actionable target."""
    env = render_thread(
        conversation_id=OTHER,
        label="Bob",
        messages=[_msg(text="hi", timestamp=1000)],
        account=ACCOUNT,
        started_at_dt=FIXED_TIME,
    )
    ctx = _buttons(env, "react")[0]["context"]
    assert ctx["target_author"] == OTHER
    assert ctx["target_timestamp"] == 1000
    assert ctx["target_timestamp"] != 0
    assert ctx["target_author"] != ""
