"""Central HTTP channel server: streamable-HTTP MCP with agent identity.

Runs channel mode over the streamable-HTTP transport (SPEC-0002 / ADR-0002):
one signal-mcp serves many agents, each session identifying itself with the
``X-Signal-Agent-Id`` request header. Inbound replies are routed to the
originating agent's live sessions only (see :mod:`signal_mcp.routing` and
the dispatch in :mod:`signal_mcp.channel`).

Auth is a single static bearer token verified by fastmcp — the server can
send Signal messages as the operator, so an unauthenticated network listener
is a phishing tool. TLS is the reverse proxy's job when exposed across hosts.

# @joestump-agent 09/03/2026 - Initial implementation of SPEC-0002.
"""

import asyncio
import contextlib
import logging
from typing import Any

import uvicorn
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.middleware import Middleware as StarletteMiddleware
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.types import InitializeResult

from signal_mcp import channel as channel_mod
from signal_mcp.config import config
from signal_mcp.routing import (
    get_session_registry,
    normalize_agent_id,
)

logger = logging.getLogger(__name__)

AGENT_ID_HEADER = "x-signal-agent-id"

# Request body cap (bytes). Outbound attachments are passed as local paths or
# http(s) URLs — never inline bytes — so nothing needs a higher limit.
MAX_BODY_BYTES = 1024 * 1024

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

ROUTING_INSTRUCTIONS_TEMPLATE = """

You are connected to this Signal channel server as agent "{agent_id}".
Notifications addressed to you carry ``route_status: "routed"`` in their
``meta`` — a reply the operator typed to a message this agent sent. A
notification with ``route_status: "agent_offline"`` or ``unknown`` is a
reply to another agent's message that you are receiving as a fallback:
``routed_agent`` names the agent it was meant for, and ``in_reply_to_text``
quotes the message so you can act with that context. The immediate
acknowledgement rule applies to routed and fallback deliveries alike.
"""


class AgentIdentityMiddleware(Middleware):
    """Capture ``X-Signal-Agent-Id`` at ``initialize`` and register the session.

    The header rides every HTTP request, but ``on_initialize`` is the one
    hook that fires exactly once per MCP session, so it is where the
    session joins the registry — including before its first tool call,
    which is what lets the default agent receive unrouted traffic it never
    asked for. A session without the header is registered under its MCP
    session id.

    The initialize *result* is also rewritten so the instructions name the
    session's own agent id and explain the ``route_status`` values
    (SPEC-0002 REQ "Routing Disclosure in Channel Instructions").
    """

    async def on_initialize(
        self, context: MiddlewareContext[Any], call_next: Any
    ) -> Any:
        result = await call_next(context)

        headers = {}
        with contextlib.suppress(Exception):
            headers = get_http_headers() or {}
        raw_agent = headers.get(AGENT_ID_HEADER)
        fastmcp_context = context.fastmcp_context
        session = fastmcp_context.session if fastmcp_context else None
        session_id = fastmcp_context.session_id if fastmcp_context else None
        if session is None or not session_id:
            # No live session to register (defensive: initialize always has
            # one in practice). Leave the result untouched.
            return result

        agent_id = normalize_agent_id(raw_agent) or session_id
        get_session_registry().register(agent_id, session_id, session)

        if isinstance(result, InitializeResult):
            result.instructions = (
                channel_mod.CHANNEL_INSTRUCTIONS
                + ROUTING_INSTRUCTIONS_TEMPLATE.format(agent_id=agent_id)
            )
        return result


class _SecurityASGIMiddleware:
    """ASGI middleware: security headers on every response, bounded bodies.

    Request bodies over :data:`MAX_BODY_BYTES` are rejected with 413 before
    reaching the MCP session manager, so a huge POST can never pin server
    memory (SPEC-0002 Security Requirements).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        content_length = headers.get("content-length")
        if content_length and content_length.isdigit():
            if int(content_length) > MAX_BODY_BYTES:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [
                            (k.lower().encode(), v.encode())
                            for k, v in SECURITY_HEADERS.items()
                        ]
                        + [(b"content-length", b"0")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return

        async def send_with_security_headers(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.decode("latin-1").lower() for k, _ in headers}
                headers.extend(
                    (k.lower().encode(), v.encode())
                    for k, v in SECURITY_HEADERS.items()
                    if k.lower() not in present
                )
                message = dict(message, headers=headers)
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


def build_auth() -> StaticTokenVerifier | None:
    """Build the static bearer-token verifier, or ``None`` when auth is off.

    Config validation has already guaranteed this is only called with a
    token set, or with ``--allow-unauthenticated`` on a loopback bind.
    """
    if not config.auth_token:
        logger.warning(
            "HTTP channel server running WITHOUT authentication on "
            f"{config.http_host}:{config.http_port} (--allow-unauthenticated)"
        )
        return None
    return StaticTokenVerifier(
        tokens={config.auth_token: {"client_id": "signal-mcp-operator"}}
    )


def build_http_app() -> Any:
    """Build the Starlette app for the central channel server.

    Session identity and routing disclosure attach only on this path: in
    stdio mode there is one session per process and nothing to register.
    """
    channel_mod.mcp.add_middleware(AgentIdentityMiddleware())
    mcp_auth = build_auth()
    if mcp_auth is not None:
        # fastmcp consults this when building the HTTP app's auth routes.
        channel_mod.mcp.auth = mcp_auth
    return channel_mod.mcp.http_app(
        transport="http",
        middleware=[StarletteMiddleware(_SecurityASGIMiddleware)],
    )


async def run_channel_http_async() -> None:
    """Run channel mode over streamable HTTP (the central channel server).

    The forwarder task and the HTTP server share one event loop — the
    session registry and route table are mutated only from that loop — and
    both are torn down together: the forwarder is cancelled and awaited,
    the daemon client is closed, and no delivery is attempted afterwards.
    """
    app = build_http_app()
    uv_config = uvicorn.Config(
        app,
        host=config.http_host,
        port=config.http_port,
        log_level=config.log_level.lower(),
        lifespan="on",
    )
    server = uvicorn.Server(uv_config)

    logger.info(
        "HTTP channel server: agent identity via the %r header, default "
        "agent %r, routing %s",
        AGENT_ID_HEADER,
        config.default_agent or "(none — fan out to all sessions)",
        "enabled" if config.routing_enabled else "disabled",
    )

    async def _serve() -> None:
        # uvicorn's serve() runs the Starlette lifespan (the MCP session
        # manager) and blocks until the server exits.
        await server.serve()

    forwarder = asyncio.create_task(
        channel_mod._forward_channel_messages(_NoopWriteStream())
    )
    try:
        await _serve()
    finally:
        forwarder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await forwarder
        from signal_mcp.rpc import get_client

        await get_client().close()


class _NoopWriteStream:
    """Stdio write-stream stand-in for the HTTP forwarder.

    In HTTP mode every delivery goes through the session registry
    (``config.routing_enabled`` is true), so the forwarder never falls back
    to the stdio write stream. This sink exists only so the stdio code path
    of the shared forwarder has something to hold; if it is ever used it
    means a routing bug, and it logs loudly.
    """

    async def send(self, msg: Any) -> None:
        logger.error(
            "HTTP channel forwarder attempted a stdio write — routing should "
            "have claimed this notification; dropping it"
        )
