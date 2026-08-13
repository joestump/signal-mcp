"""Tests for the A2UI conversation index renderer (#63)."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

from signal_mcp.a2ui import (
    A2UI_VERSION,
    CATALOG_ID,
    render_index,
    render_thread,
)
from signal_mcp.history import ConversationSummary

FIXED_TIME = datetime(2026, 8, 12, 14, 2, 0, tzinfo=timezone.utc)
OTHER = "+11234567890"


def _summary(
    key: str = "+111",
    label: str = "Alice",
    preview: str = "hello",
    count: int = 1,
    last_activity: int | None = 1000,
) -> ConversationSummary:
    return ConversationSummary(
        key=key,
        label=label,
        preview=preview,
        message_count=count,
        last_activity=last_activity,
    )


def test_render_empty_index():
    """An empty buffer renders a valid surface with empty-state text."""
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_index(summaries=[])
    assert env["version"] == A2UI_VERSION
    assert env["updateComponents"]["catalogId"] == CATALOG_ID
    assert "No buffered conversations" in json.dumps(env)


def test_render_three_conversations():
    """Three conversations render as rows, MRU first."""
    summaries = [
        _summary(
            key="+333", label="Charlie", preview="hi", count=3, last_activity=3000
        ),
        _summary(key="+222", label="Bob", preview="yo", count=2, last_activity=2000),
        _summary(
            key="+111", label="Alice", preview="hello", count=1, last_activity=1000
        ),
    ]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_index(summaries=summaries)
    comp_str = json.dumps(env, ensure_ascii=False)
    assert "Charlie" in comp_str
    assert "Bob" in comp_str
    assert "Alice" in comp_str
    assert "hi" in comp_str
    assert "hello" in comp_str
    # Order is preserved (MRU first).
    charlie_pos = comp_str.index("Charlie")
    bob_pos = comp_str.index("Bob")
    alice_pos = comp_str.index("Alice")
    assert charlie_pos < bob_pos < alice_pos


def test_scope_caption_always_present():
    """Every index surface carries the scope disclosure."""
    for summaries in ([], [_summary()]):
        with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
            env = render_index(summaries=summaries)
        comp_str = json.dumps(env)
        assert "This instance's view since" in comp_str
        assert "2026-08-12 14:02 UTC" in comp_str
        assert "the phone is the complete record" in comp_str


def test_scope_caption_shared_with_thread():
    """The index and thread renderers share the same caption helper."""
    started = FIXED_TIME
    index_env = render_index(summaries=[], started_at_dt=started)
    thread_env = render_thread(
        conversation_id="+123",
        label="Test",
        messages=[],
        account="+456",
        started_at_dt=started,
    )

    # Extract the scope caption from both.
    def find_scope(env):
        for comp in env["updateComponents"]["components"]:
            if comp.get("id") == "scope":
                return comp["text"]
        return None

    assert find_scope(index_env) == find_scope(thread_env)


def test_hostile_label_is_inert():
    """A conversation label that looks like A2UI JSON renders as text."""
    hostile = json.dumps({"updateComponents": {"surfaceId": "evil"}})
    summaries = [_summary(label=hostile)]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_index(summaries=summaries)
    assert env["updateComponents"]["surfaceId"] == "index"
    comp_str = json.dumps(env)
    assert "evil" in comp_str


def test_message_count_in_meta():
    """Each row shows the message count."""
    summaries = [_summary(count=5)]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_index(summaries=summaries)
    comp_str = json.dumps(env)
    assert "5 messages" in comp_str


def test_single_message_count_singular():
    """A conversation with 1 message uses singular 'message'."""
    summaries = [_summary(count=1)]
    with patch("signal_mcp.a2ui.started_at", return_value=FIXED_TIME):
        env = render_index(summaries=summaries)
    comp_str = json.dumps(env)
    assert "1 message" in comp_str
    assert "1 messages" not in comp_str


def test_unformattable_last_activity_leaves_no_dangling_separator():
    """A timestamp _format_timestamp cannot render is omitted, separator and all.

    last_activity is sender-derived, so an out-of-range value survives as a
    non-None int. Testing the raw field rather than the formatted result left
    the meta line reading "3 messages · " with nothing after the separator.
    """
    env = render_index(
        summaries=[
            ConversationSummary(
                key=OTHER,
                label="Bob",
                preview="hi",
                message_count=3,
                last_activity=10**20,
            )
        ],
        started_at_dt=FIXED_TIME,
    )
    components = env["updateComponents"]["components"]
    meta = next(c for c in components if c["id"] == "conv-meta-0")
    assert meta["text"] == "3 messages"


def test_valid_last_activity_still_rendered():
    """A normal timestamp still appears after the count."""
    env = render_index(
        summaries=[
            ConversationSummary(
                key=OTHER,
                label="Bob",
                preview="hi",
                message_count=3,
                last_activity=1786000000000,
            )
        ],
        started_at_dt=FIXED_TIME,
    )
    components = env["updateComponents"]["components"]
    meta = next(c for c in components if c["id"] == "conv-meta-0")
    assert meta["text"].startswith("3 messages · ")
    assert meta["text"].rstrip() != "3 messages ·"
