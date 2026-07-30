"""Regression tests for issue #45: unknown MCP methods must not wedge the server.

Newer MCP clients (the modelcontextprotocol go-sdk since SEP-2575) open every
connection with a ``server/discover`` request *before* ``initialize``. On mcp
1.6.0 that unknown method raised a pydantic ``ValidationError`` inside the
SDK's receive loop, killing the session without ever sending a response —
and in channel mode the process stayed alive, so clients saw neither a reply
nor EOF and waited out their entire connect timeout (60s+ of a frozen
harness). mcp >= 1.29 answers with a JSON-RPC error instead, which such
clients treat as "discover unsupported" and fall back to the legacy
initialize handshake.

These tests drive the real low-level server over in-memory streams — the
same stream contract ``stdio_server`` provides — so they fail by timeout on
an SDK that swallows unknown methods, and fail on type errors if the wire
types drift from what the stdio transport carries.
"""

import asyncio

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)

from signal_mcp.channel import _channel_notification
from signal_mcp.tools import mcp


def _request(req_id: int, method: str, params: dict) -> SessionMessage:
    return SessionMessage(
        JSONRPCMessage(
            root=JSONRPCRequest(jsonrpc="2.0", id=req_id, method=method, params=params)
        )
    )


def test_unknown_method_gets_error_and_session_survives():
    """A ``server/discover`` probe draws an error response, not a dead session.

    Mirrors the go-sdk's connect sequence: the unknown request arrives first,
    then the client falls back to the legacy ``initialize`` handshake, which
    must still succeed on the same session.
    """

    async def _runner():
        c2s_send, c2s_recv = anyio.create_memory_object_stream[SessionMessage](16)
        s2c_send, s2c_recv = anyio.create_memory_object_stream[SessionMessage](16)
        init_options = mcp._mcp_server.create_initialization_options(
            experimental_capabilities={"claude/channel": {}}
        )
        server_task = asyncio.create_task(
            mcp._mcp_server.run(c2s_recv, s2c_send, init_options)
        )
        try:
            await c2s_send.send(
                _request(
                    1,
                    "server/discover",
                    {
                        "_meta": {
                            "io.modelcontextprotocol/protocol-version": "2026-07-28"
                        }
                    },
                )
            )
            resp = await asyncio.wait_for(s2c_recv.receive(), timeout=5)
            root = resp.message.root
            assert isinstance(root, JSONRPCError), (
                f"unknown method must draw a JSON-RPC error response, got {root!r}"
            )
            assert root.id == 1

            # The session must survive the unknown method: the initialize
            # handshake the client falls back to still completes.
            await c2s_send.send(
                _request(
                    2,
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "probe", "version": "0"},
                    },
                )
            )
            resp2 = await asyncio.wait_for(s2c_recv.receive(), timeout=5)
            root2 = resp2.message.root
            assert isinstance(root2, JSONRPCResponse), (
                f"initialize after a rejected unknown method must succeed, got {root2!r}"
            )
            assert root2.id == 2
            assert root2.result["serverInfo"]["name"] == "signal-cli"
        finally:
            await c2s_send.aclose()
            try:
                await asyncio.wait_for(server_task, timeout=5)
            except Exception:  # noqa: BLE001
                server_task.cancel()

    asyncio.run(_runner())


def test_channel_notification_is_a_session_message():
    """The forwarder's wire type matches what the stdio write stream carries.

    The SDK's stdout writer dereferences ``session_message.message``, so a
    bare ``JSONRPCMessage`` pushed by the forwarder would crash the
    transport. Pin the wrapper type here since the forwarder unit tests use
    fakes that would accept either.
    """
    notification = _channel_notification("hello", {"sender": "+15551234567"})
    assert isinstance(notification, SessionMessage)
    root = notification.message.root
    assert isinstance(root, JSONRPCNotification)
    assert root.method == "notifications/claude/channel"
