"""Tests for A2UI resource registration (#66)."""

import asyncio
import json
from unittest.mock import patch


from signal_mcp.config import config
from signal_mcp.history import ConversationBuffer
from signal_mcp.tools import mcp


def _templates():
    """The advertised resource templates, keyed by URI template."""
    return {str(t.uri_template): t for t in asyncio.run(mcp.list_resource_templates())}


def test_thread_resources_registered():
    """Both signal:// and mcp:// thread URIs are registered."""
    templates = _templates()
    assert "signal://conversation/{id}/a2ui{?w}" in templates
    assert "mcp://signal/conversation/{id}/a2ui{?w}" in templates


def test_index_resources_registered():
    """Both signal:// and mcp:// index URIs are registered.

    Declaring the optional ``{?w}`` width hint makes the index a template
    even though it has no path parameter, so it advertises under
    resourceTemplates/list rather than resources/list.
    """
    templates = _templates()
    assert "signal://conversations/a2ui{?w}" in templates
    assert "mcp://signal/conversations/a2ui{?w}" in templates


def test_all_a2ui_surfaces_are_templates():
    """No A2UI surface advertises as a concrete resource."""
    assert asyncio.run(mcp.list_resources()) == []
    assert len(_templates()) == 4


def test_mime_type_is_a2ui():
    """Every registered resource declares application/a2ui+json."""
    for uri, template in _templates().items():
        assert template.mime_type == "application/a2ui+json", uri


def test_audience_is_user():
    """Every registered resource has audience: ['user']."""
    for uri, template in _templates().items():
        assert template.annotations is not None, uri
        assert "user" in (template.annotations.audience or []), uri


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


def test_percent_encoded_group_id_round_trips_through_the_uri():
    """A percent-encoded group id in the URI reaches the handler decoded.

    fastmcp decodes template parameters exactly once on the way in, so the
    handler no longer decodes for itself — this reads through the real URI
    path to pin the round trip end to end rather than calling the handler.
    """
    group_id = "aGVsbG8="  # base64 with =
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
        content = asyncio.run(
            mcp.read_resource("signal://conversation/aGVsbG8%3D/a2ui")
        ).contents[0]

    assert "group hi" in content.content


def test_encoded_slash_in_group_id_round_trips():
    """An encoded slash in a group id stays inside the matched segment.

    ``{id}`` is a simple RFC 6570 placeholder matching one segment, and
    fastmcp matches the still-encoded path before unquoting the captured
    value — so ``%2F`` does not split the segment and decodes to a literal
    ``/``. Real base64 group ids contain ``/``, so this is the common case,
    and a matcher that decoded before matching would break every group
    thread read.
    """
    group_id = "aGVs/bG8="  # base64 with / and =
    buf = ConversationBuffer()
    from signal_mcp.history import BufferedMessage

    buf.record(
        BufferedMessage(
            conversation_key=group_id,
            direction="inbound",
            sender_id="+123",
            sender_name="Alice",
            text="group with slash",
            timestamp=1000,
        )
    )
    with (
        patch("signal_mcp.tools.history_buffer", buf),
        patch.object(config, "account", "+456"),
    ):
        content = asyncio.run(
            mcp.read_resource("signal://conversation/aGVs%2FbG8%3D/a2ui")
        ).contents[0]

    assert "group with slash" in content.content


def test_handler_does_not_decode_its_argument_again():
    """The handler treats its id as already-decoded.

    Decoding a second time would corrupt any conversation key containing a
    literal '%', which is what makes the removed unquote() a hazard rather
    than a no-op.
    """
    key = "group%3Dnot-encoded"
    buf = ConversationBuffer()
    from signal_mcp.history import BufferedMessage

    buf.record(
        BufferedMessage(
            conversation_key=key,
            direction="inbound",
            sender_id="+123",
            sender_name="Alice",
            text="literal percent",
            timestamp=1000,
        )
    )
    with (
        patch("signal_mcp.tools.history_buffer", buf),
        patch.object(config, "account", "+456"),
    ):
        result = asyncio.run(_call_thread_handler(key))

    assert "literal percent" in result


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


def _body(result) -> str:
    """The single text body of a surface result.

    ``ResourceContent.content`` is typed ``str | bytes``; every A2UI surface
    serializes JSON, so anything else is a bug worth failing on loudly.
    """
    content = result.contents[0].content
    assert isinstance(content, str)
    return content


async def _call_thread_handler(id: str) -> str:
    """Invoke the thread surface resource handler, returning its JSON body."""
    from signal_mcp.tools import thread_surface

    return _body(await thread_surface(id))


async def _call_index_handler() -> str:
    """Invoke the conversations surface resource handler, returning its body."""
    from signal_mcp.tools import conversations_surface

    return _body(await conversations_surface())


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
        env = json.loads(asyncio.run(_call_thread_handler("GID==")))

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


def _read(uri: str):
    """Read *uri* against a buffer holding one message from Alice."""
    with (
        patch("signal_mcp.tools.history_buffer", _buffer_with_message()),
        patch.object(config, "account", "+456"),
    ):
        return asyncio.run(mcp.read_resource(uri)).contents[0]


def test_read_index_with_width_hint():
    """A ?w=<width> hint on the index URI resolves.

    A2UI-capable hosts append an optional width hint to any /a2ui URI. The
    URI declares it as RFC 6570 ``{?w}``, which is what lets fastmcp match
    the path and bind the hint instead of failing the lookup.
    """
    content = _read("signal://conversations/a2ui?w=112")
    assert content.mime_type == "application/a2ui+json"
    env = json.loads(content.content)
    assert env["version"] == "v0.9"
    assert "Alice" in content.content


def test_read_index_with_width_hint_mcp_scheme():
    """The mcp:// twin of the index URI takes the hint too."""
    content = _read("mcp://signal/conversations/a2ui?w=112")
    assert content.mime_type == "application/a2ui+json"
    assert "Alice" in content.content


def test_read_thread_template_with_width_hint():
    """A ?w=<width> hint resolves against the thread template as well."""
    content = _read("signal://conversation/%2B123/a2ui?w=80")
    assert content.mime_type == "application/a2ui+json"
    assert "hello" in content.content


def test_read_without_query_still_resolves():
    """The bare URI keeps working unchanged."""
    assert "Alice" in _read("signal://conversations/a2ui").content


def test_width_hint_is_extracted_from_the_uri():
    """The declared ``{?w}`` binds the hint rather than failing the match.

    Asserted through the template's own matcher — the server uses the same
    call to resolve a read. The handler ignores ``w``, so there is no
    observable rendering difference to assert on instead.
    """
    template = _templates()["signal://conversations/a2ui{?w}"]
    assert template.matches("signal://conversations/a2ui?w=112") == {"w": "112"}
    assert template.matches("signal://conversations/a2ui") == {}


def test_unrecognized_query_parameter_is_ignored():
    """A query param the URI does not declare does not break the read."""
    assert "Alice" in _read("signal://conversations/a2ui?bogus=1").content


def test_unknown_resource_with_query_still_errors():
    """Tolerating the hint does not turn unknown URIs into known ones."""
    import pytest
    from fastmcp.exceptions import NotFoundError

    with pytest.raises(NotFoundError, match="Unknown resource"):
        asyncio.run(mcp.read_resource("signal://nope/a2ui?w=112"))
