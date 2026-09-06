"""End-to-end test (SPEC-0002 #96): two agent sessions over HTTP.

Starts the real streamable-HTTP channel app under uvicorn inside the test's
event loop (the same shape ``run_channel_http_async`` uses), connects two
fastmcp clients carrying different ``X-Signal-Agent-Id`` headers, and covers
the whole HTTP surface in one server session: reply routing to the
originating agent only, route recording on send, auth rejection, security
headers, and the request body cap.

The FastMCP instance is process-global, so the scenarios deliberately share
one server run rather than starting uvicorn per test.
"""

import asyncio
import contextlib
import json
import socket
from unittest.mock import patch

import httpx
import uvicorn
from mcp.types import JSONRPCNotification, ServerNotificationType
from pydantic import RootModel

from signal_mcp import rpc, routing
from signal_mcp.channel import _forward_channel_messages
from signal_mcp.config import config
from signal_mcp.http_channel import (
    MAX_BODY_BYTES,
    SECURITY_HEADERS,
    _NoopWriteStream,
    build_http_app,
)
from signal_mcp.parse import _envelope_to_response

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ACCOUNT = "+15550000000"
OPERATOR = "+15550001111"
TOKEN = "test-token"

# The stock mcp SDK client drops unknown notification methods during
# validation; real hosts (Crush, Claude Code) consume raw JSON. Widen the
# union so this test can observe the claude/channel notification.
_PermissiveNotification = RootModel[ServerNotificationType | JSONRPCNotification]


class _QueueClient:
    """Minimal rpc client stand-in: queued messages, recorded calls."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.calls: list[tuple[str, dict]] = []

    async def connect(self) -> None:
        pass

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        return {"timestamp": 1234}

    async def next_message(self, timeout: float):
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _reply_envelope():
    """Operator reply quoting timestamp 1700 (sent by the account)."""
    return _envelope_to_response(
        {
            "envelope": {
                "source": OPERATOR,
                "timestamp": 1800,
                "dataMessage": {
                    "message": "yes, roll it out",
                    "timestamp": 1800,
                    "quote": {
                        "id": 1700,
                        "author": ACCOUNT,
                        "text": "deploy finished",
                    },
                },
            }
        }
    )


def _client(agent_id: str, port: int, collector=None) -> Client:
    return Client(
        transport=StreamableHttpTransport(
            url=f"http://127.0.0.1:{port}/mcp/",
            headers={
                "X-Signal-Agent-Id": agent_id,
                "Authorization": f"Bearer {TOKEN}",
            },
        ),
        message_handler=collector,
        auth=TOKEN,
        timeout=10,
    )


class _Collector:
    """Gathers claude/channel notifications arriving at a client."""

    def __init__(self) -> None:
        self.notifications: list = []

    def __call__(self, message) -> None:
        payload = getattr(message, "root", message)
        if getattr(payload, "method", None) == "notifications/claude/channel":
            self.notifications.append(payload)


def _configure(monkeypatch, port: int) -> None:
    monkeypatch.setattr(config, "account", ACCOUNT)
    monkeypatch.setattr(config, "operator", OPERATOR)
    monkeypatch.setattr(config, "channel_mode", True)
    monkeypatch.setattr(config, "transport", "http")
    monkeypatch.setattr(config, "http_host", "127.0.0.1")
    monkeypatch.setattr(config, "http_port", port)
    monkeypatch.setattr(config, "auth_token", TOKEN)
    monkeypatch.setattr(config, "default_agent", "default-agent")
    monkeypatch.setattr(config, "trusted_senders", frozenset({OPERATOR}))
    routing.reset_routing_state()


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


def test_http_channel_end_to_end(monkeypatch):
    # Widen the client SDK's notification union for observation (raw-JSON
    # hosts need no such accommodation).
    import mcp.types as mcp_types

    monkeypatch.setattr(
        mcp_types.ServerNotification,
        "model_validate",
        classmethod(lambda cls, data: _PermissiveNotification.model_validate(data)),
    )

    port = _free_port()
    _configure(monkeypatch, port)
    asyncio.run(_scenario(port))


async def _scenario(port: int) -> None:
    # Agent A "sent" timestamp 1700 before the reply arrives.
    routing.get_route_table().record(1700, "agent-a", "conv", "deploy finished")

    fake_rpc = _QueueClient()
    collector_a, collector_b = _Collector(), _Collector()

    app = build_http_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, lifespan="on", log_level="error"
        )
    )
    server_task = asyncio.create_task(server.serve())
    try:
        assert await _wait_for(lambda: server.started), "server did not start"

        # -- Auth: a request without the token is rejected. ----------------
        async with httpx.AsyncClient() as http:
            unauthenticated = await http.post(
                f"http://127.0.0.1:{port}/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"Accept": "application/json, text/event-stream"},
                follow_redirects=True,
            )
        assert unauthenticated.status_code == 401

        # -- Security headers on every response. ---------------------------
        async with httpx.AsyncClient() as http:
            probed = await http.get(
                f"http://127.0.0.1:{port}/mcp",
                headers={"Authorization": f"Bearer {TOKEN}"},
                follow_redirects=True,
            )
        for header, value in SECURITY_HEADERS.items():
            assert probed.headers.get(header) == value

        # -- Request body cap: oversized bodies are rejected with 413. -----
        oversized = json.dumps({"x": "y" * (MAX_BODY_BYTES + 1)})
        async with httpx.AsyncClient() as http:
            rejected = await http.post(
                f"http://127.0.0.1:{port}/mcp",
                content=oversized,
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(oversized)),
                },
                follow_redirects=True,
            )
        assert rejected.status_code == 413

        # -- Two agent sessions, one routed reply, one recorded send. ------
        client_a = _client("agent-a", port, collector_a)
        client_b = _client("agent-b", port, collector_b)
        async with client_a, client_b:
            forwarder = asyncio.create_task(
                _forward_channel_messages(_NoopWriteStream())
            )
            try:
                with patch.object(rpc, "client", fake_rpc):
                    # Both sessions registered at initialize via the
                    # X-Signal-Agent-Id header.
                    assert await _wait_for(
                        lambda: routing.get_session_registry().has_live("agent-a")
                        and routing.get_session_registry().has_live("agent-b")
                    ), "sessions were not registered"

                    # A `send` from agent-a's session records the route.
                    await client_a.call_tool("send", {"message": "deploy finished"})
                    assert any(method == "send" for method, _ in fake_rpc.calls)
                    entry = routing.get_route_table().lookup(1234)
                    assert entry is not None
                    assert entry.agent_id == "agent-a"

                    # The operator's reply quoting 1700 reaches agent A only.
                    await fake_rpc.queue.put(_reply_envelope())
                    got_a = await _wait_for(lambda: bool(collector_a.notifications))
                    got_b = await _wait_for(
                        lambda: bool(collector_b.notifications), timeout=1.0
                    )

                assert got_a, "agent A did not receive the routed reply"
                assert not got_b, "agent B must not receive agent A's reply"
                notification = collector_a.notifications[0]
                assert notification.params["meta"]["route_status"] == "routed"
                assert notification.params["meta"]["routed_agent"] == "agent-a"
                assert notification.params["content"] == "yes, roll it out"

                # One read receipt for the delivered reply.
                receipts = [m for m, _ in fake_rpc.calls if m == "sendReceipt"]
                assert receipts == ["sendReceipt"]
            finally:
                forwarder.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await forwarder
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await server_task


def test_chunked_body_rejected_with_413():
    """A chunked request carries no content-length, so the cap rejects it
    outright — every accepted body is bounded (SPEC-0002 Security)."""
    from signal_mcp.http_channel import _SecurityASGIMiddleware

    async def run() -> tuple[int, list[tuple[bytes, bytes]]]:
        sent: list[dict] = []

        async def app(scope, receive, send):  # pragma: no cover - must not run
            raise AssertionError("chunked request reached the app")

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        middleware = _SecurityASGIMiddleware(app)
        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "headers": [
                    (b"transfer-encoding", b"chunked"),
                    (b"content-type", b"application/json"),
                ],
            },
            receive,
            send,
        )
        status = sent[0]["status"]
        headers = [(k, v) for k, v in sent[0]["headers"]]
        return status, headers

    status, headers = asyncio.run(run())
    assert status == 413
    for name, value in SECURITY_HEADERS.items():
        assert (name.lower().encode(), value.encode()) in headers
