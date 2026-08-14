"""Tests for A2UI resource registration (#66)."""

import json
from unittest.mock import patch


from signal_mcp.config import config
from signal_mcp.history import ConversationBuffer
from signal_mcp.tools import mcp


def _get_resource_manager():
    """Access the FastMCP resource manager."""
    return mcp._resource_manager


def test_thread_resources_registered():
    """Both signal:// and mcp:// thread URIs are registered."""
    rm = _get_resource_manager()
    templates = rm._templates
    assert "signal://conversation/{id}/a2ui" in templates
    assert "mcp://signal/conversation/{id}/a2ui" in templates


def test_index_resources_registered():
    """Both signal:// and mcp:// index URIs are registered."""
    rm = _get_resource_manager()
    resources = rm._resources
    assert "signal://conversations/a2ui" in resources
    assert "mcp://signal/conversations/a2ui" in resources


def test_mime_type_is_a2ui():
    """Every registered resource declares application/a2ui+json."""
    rm = _get_resource_manager()
    for uri, template in rm._templates.items():
        assert template.mime_type == "application/a2ui+json", uri
    for uri, resource in rm._resources.items():
        assert resource.mime_type == "application/a2ui+json", uri


def test_audience_is_user():
    """Every registered resource has audience: ['user']."""
    rm = _get_resource_manager()
    for uri, template in rm._templates.items():
        assert template.annotations is not None, uri
        assert "user" in template.annotations.audience, uri
    for uri, resource in rm._resources.items():
        assert resource.annotations is not None, uri
        assert "user" in resource.annotations.audience, uri


def test_thread_handler_renders_snapshot():
    """The thread handler snapshots the buffer and returns a valid envelope."""
    buf = ConversationBuffer()
    from signal_mcp.history import BufferedMessage

    buf.record(
        BufferedMessage(
            conversation_key="+123",
            direction="inbound",
            sender_id="+123",
            sender_name="Alice",
            text="hello",
            timestamp=1000,
        )
    )
    with (
        patch("signal_mcp.tools.history_buffer", buf),
        patch.object(config, "account", "+456"),
    ):
        # Call the handler directly.
        import asyncio

        result = asyncio.run(_call_thread_handler("+123"))
    env = json.loads(result)
    assert env["version"] == "v0.9"
    assert "hello" in json.dumps(env)


def test_thread_handler_empty_conversation():
    """An unknown id renders an empty-state surface, not an error."""
    buf = ConversationBuffer()
    with (
        patch("signal_mcp.tools.history_buffer", buf),
        patch.object(config, "account", "+456"),
    ):
        import asyncio

        result = asyncio.run(_call_thread_handler("+999"))
    env = json.loads(result)
    assert "No buffered messages" in json.dumps(env)


def test_thread_handler_percent_decodes_group_id():
    """A percent-encoded group id round-trips."""
    group_id = "aGVsbG8="  # base64 with =
    encoded = "aGVsbG8%3D"
    buf = ConversationBuffer()
    from signal_mcp.history import BufferedMessage

    buf.record(
        BufferedMessage(
            conversation_key=group_id,
            direction="inbound",
            sender_id="+123",
            sender_name="Alice",
            text="group hi",
            timestamp=1000,
        )
    )
    with (
        patch("signal_mcp.tools.history_buffer", buf),
        patch.object(config, "account", "+456"),
    ):
        import asyncio

        result = asyncio.run(_call_thread_handler(encoded))
    env = json.loads(result)
    assert "group hi" in json.dumps(env)


def test_index_handler_renders_conversations():
    """The index handler returns a valid envelope listing conversations."""
    buf = ConversationBuffer()
    from signal_mcp.history import BufferedMessage

    buf.record(
        BufferedMessage(
            conversation_key="+123",
            direction="inbound",
            sender_id="+123",
            sender_name="Alice",
            text="hello",
            timestamp=1000,
        )
    )
    with patch("signal_mcp.tools.history_buffer", buf):
        import asyncio

        result = asyncio.run(_call_index_handler())
    env = json.loads(result)
    assert env["version"] == "v0.9"
    assert "Alice" in json.dumps(env)


def test_read_does_not_touch_daemon():
    """A resource read never calls the RPC client."""
    buf = ConversationBuffer()
    with (
        patch("signal_mcp.tools.history_buffer", buf),
        patch.object(config, "account", "+456"),
        patch("signal_mcp.tools.get_client") as mock_client,
    ):
        import asyncio

        asyncio.run(_call_thread_handler("+123"))
        asyncio.run(_call_index_handler())
    mock_client.assert_not_called()


async def _call_thread_handler(id: str) -> str:
    """Invoke the thread surface resource handler."""
    from signal_mcp.tools import thread_surface

    return await thread_surface(id)


async def _call_index_handler() -> str:
    """Invoke the conversations surface resource handler."""
    from signal_mcp.tools import conversations_surface

    return await conversations_surface()


def test_thread_heading_matches_the_index_label():
    """The thread heading names the conversation the way the index row does.

    The handler passed the raw conversation key as the label, so a DM the
    index listed as "Alice" opened onto a thread headed by her bare phone
    number.
    """
    import asyncio

    from signal_mcp.history import BufferedMessage

    buf = ConversationBuffer()
    buf.record(
        BufferedMessage(
            conversation_key="+123",
            direction="inbound",
            sender_id="+123",
            sender_name="Alice",
            text="hello",
            timestamp=1000,
        )
    )
    with (
        patch("signal_mcp.tools.history_buffer", buf),
        patch.object(config, "account", "+456"),
    ):
        index_label = buf.conversations()[0].label
        env = json.loads(asyncio.run(_call_thread_handler("+123")))

    heading = next(
        c for c in env["updateComponents"]["components"] if c["id"] == "heading"
    )
    assert index_label == "Alice"
    assert heading["text"] == "Alice"


def test_group_thread_heading_falls_back_to_the_group_id():
    """A group has no contact name, so the key remains the heading."""
    import asyncio

    from signal_mcp.history import BufferedMessage

    buf = ConversationBuffer()
    buf.record(
        BufferedMessage(
            conversation_key="GID==",
            direction="inbound",
            sender_id="+123",
            sender_name="Alice",
            text="team update",
            timestamp=1000,
        )
    )
    with (
        patch("signal_mcp.tools.history_buffer", buf),
        patch.object(config, "account", "+456"),
    ):
        env = json.loads(asyncio.run(_call_thread_handler("GID%3D%3D")))

    heading = next(
        c for c in env["updateComponents"]["components"] if c["id"] == "heading"
    )
    assert heading["text"] == "GID=="


def test_unknown_conversation_heading_falls_back_to_the_key():
    """An id with nothing buffered still gets a heading, not an exception."""
    import asyncio

    with (
        patch("signal_mcp.tools.history_buffer", ConversationBuffer()),
        patch.object(config, "account", "+456"),
    ):
        env = json.loads(asyncio.run(_call_thread_handler("+999")))

    heading = next(
        c for c in env["updateComponents"]["components"] if c["id"] == "heading"
    )
    assert heading["text"] == "+999"


def _buffer_with_message():
    """A buffer holding one inbound DM from Alice."""
    from signal_mcp.history import BufferedMessage, ConversationBuffer

    buf = ConversationBuffer()
    buf.record(
        BufferedMessage(
            conversation_key="+123",
            direction="inbound",
            sender_id="+123",
            sender_name="Alice",
            text="hello",
            timestamp=1000,
        )
    )
    return buf


def test_read_index_with_width_hint():
    """A ?w=<width> hint on the index URI no longer fails resolution.

    A2UI-capable hosts append an optional width hint to any /a2ui URI; the
    SDK matches resources on the full URI string, so the hint used to make
    every read fail with "Unknown resource".
    """
    import asyncio

    with (
        patch("signal_mcp.tools.history_buffer", _buffer_with_message()),
        patch.object(config, "account", "+456"),
    ):
        contents = asyncio.run(mcp.read_resource("signal://conversations/a2ui?w=112"))

    (content,) = contents
    assert content.mime_type == "application/a2ui+json"
    env = json.loads(content.content)
    assert env["version"] == "v0.9"
    assert "Alice" in content.content


def test_read_index_with_width_hint_mcp_scheme():
    """The mcp:// twin of the index URI tolerates the hint too."""
    import asyncio

    with (
        patch("signal_mcp.tools.history_buffer", _buffer_with_message()),
        patch.object(config, "account", "+456"),
    ):
        contents = asyncio.run(
            mcp.read_resource("mcp://signal/conversations/a2ui?w=112")
        )

    (content,) = contents
    assert content.mime_type == "application/a2ui+json"
    assert "Alice" in content.content


def test_read_thread_template_with_width_hint():
    """A ?w=<width> hint resolves against the thread template as well."""
    import asyncio

    with (
        patch("signal_mcp.tools.history_buffer", _buffer_with_message()),
        patch.object(config, "account", "+456"),
    ):
        contents = asyncio.run(
            mcp.read_resource("signal://conversation/%2B123/a2ui?w=80")
        )

    (content,) = contents
    assert content.mime_type == "application/a2ui+json"
    assert "hello" in content.content


def test_read_fragment_is_also_stripped():
    """A #fragment on the URI resolves like the bare URI."""
    import asyncio

    with (
        patch("signal_mcp.tools.history_buffer", _buffer_with_message()),
        patch.object(config, "account", "+456"),
    ):
        contents = asyncio.run(mcp.read_resource("signal://conversations/a2ui#anchor"))

    (content,) = contents
    assert "Alice" in content.content


def test_read_without_query_still_resolves():
    """The bare URI keeps working unchanged."""
    import asyncio

    with (
        patch("signal_mcp.tools.history_buffer", _buffer_with_message()),
        patch.object(config, "account", "+456"),
    ):
        contents = asyncio.run(mcp.read_resource("signal://conversations/a2ui"))

    (content,) = contents
    assert "Alice" in content.content


def test_unknown_resource_with_query_still_errors():
    """Stripping the hint does not turn unknown URIs into known ones."""
    import asyncio

    import pytest

    with pytest.raises(ValueError, match="Unknown resource"):
        asyncio.run(mcp.read_resource("signal://nope/a2ui?w=112"))
