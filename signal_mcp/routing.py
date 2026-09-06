"""Reply routing state: the route table and the session registry.

Two bounded, in-memory structures behind ADR-0002 (SPEC-0002):

* :class:`RouteTable` answers exactly one question — "which agent sent the
  Signal message with timestamp T?" — for inbound replies. It is keyed by
  outbound timestamp, bounded by TTL and entry count, and deliberately NOT
  the SPEC-0001 conversation buffer: it holds no content beyond a short
  preview and is not persisted anywhere. A restart forgets every route, and
  the unrouted fallback covers the miss.
* :class:`SessionRegistry` tracks the live MCP sessions per agent id so
  dispatch can reach the originating agent's sessions — and only them.

Both are mutated only from the server event loop (the forwarder task and
request handlers share it), per the design's concurrency contract; no locks.

# @joestump-agent 09/03/2026 - Initial implementation of SPEC-0002.
"""

import logging
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Preview cap for route entries and quoted text (bytes, truncated at a UTF-8
# character boundary).
PREVIEW_BYTES = 256

# Agent ids are opaque strings capped at 128 bytes (SPEC-0002 REQ "Agent
# Session Identity").
AGENT_ID_MAX_BYTES = 128


class UnroutableSessionError(Exception):
    """A registered session could not accept a dispatched notification.

    Raised (or recorded by the delivery loop) when the session's
    server-to-client stream is closed or otherwise refuses delivery. The
    caller should drop the session from the registry so the agent reads as
    offline on the next dispatch.
    """


class UnauthenticatedRequestError(Exception):
    """An HTTP request arrived without a valid bearer token.

    fastmcp's token verifier rejects such requests with 401 before any tool
    or middleware runs; this sentinel exists for programmatic distinction in
    the auth plumbing.
    """


def truncate_utf8(text: str, limit: int = PREVIEW_BYTES) -> str:
    """Truncate ``text`` to at most ``limit`` bytes at a UTF-8 boundary.

    Encodes to UTF-8, slices to ``limit`` bytes, and drops any trailing
    partial multi-byte character. ASCII text longer than the limit is simply
    cut at the limit.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def normalize_agent_id(value: str | None) -> str | None:
    """Normalize an agent id: trimmed, capped, empty becomes ``None``."""
    if not value:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    return truncate_utf8(trimmed, AGENT_ID_MAX_BYTES)


@dataclass
class RouteEntry:
    """One outbound message's routing record."""

    agent_id: str
    conversation_key: str
    preview: str
    recorded_at: float


@dataclass
class Dispatch:
    """The outcome of recipient resolution for one inbound message."""

    # (agent_id, session) pairs to deliver to, in registry order.
    recipients: list[tuple[str, str, object]] = field(default_factory=list)
    # routed | agent_offline | unknown | unrouted (SPEC-0002 REQ "Reply
    # Dispatch" and "Unrouted Traffic and the Default Agent").
    route_status: str = "unrouted"
    # The agent id the message was addressed to, when one was identified
    # (set for "routed" and "agent_offline"; carried through fallback).
    routed_agent: str | None = None


class RouteTable:
    """In-memory map of outbound Signal timestamp → sending agent id.

    ``OrderedDict`` under the hood: insertion order gives FIFO eviction when
    the entry cap is hit, and :meth:`record` re-inserts on timestamp
    collision so a re-sent timestamp keeps the newest entry. Expiry is lazy —
    lookups and inserts drop stale entries — so no background sweeper task
    exists to manage.
    """

    def __init__(self, ttl_seconds: float = 604800, max_entries: int = 10000):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl = float(ttl_seconds)
        self._max_entries = max_entries
        self._entries: OrderedDict[int, RouteEntry] = OrderedDict()

    def _expire(self, now: float) -> None:
        """Drop entries older than the TTL (oldest first)."""
        cutoff = now - self._ttl
        while self._entries:
            oldest = next(iter(self._entries.values()))
            if oldest.recorded_at > cutoff:
                break
            del self._entries[next(iter(self._entries))]

    def record(
        self, timestamp: int, agent_id: str, conversation_key: str, preview: str = ""
    ) -> None:
        """Record that ``agent_id`` sent the message with ``timestamp``.

        Never raises and never fails a send: oversized previews are
        truncated here, and eviction (TTL, then FIFO at the cap) keeps the
        table bounded.
        """
        try:
            now = time.time()
            self._expire(now)
            self._entries.pop(timestamp, None)
            self._entries[timestamp] = RouteEntry(
                agent_id=agent_id,
                conversation_key=conversation_key,
                preview=truncate_utf8(preview),
                recorded_at=now,
            )
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to record route", exc_info=True)

    def lookup(self, timestamp: int) -> RouteEntry | None:
        """Return the route for ``timestamp``, or ``None`` when unknown/expired."""
        now = time.time()
        self._expire(now)
        entry = self._entries.get(timestamp)
        if entry is None:
            return None
        if now - entry.recorded_at > self._ttl:
            del self._entries[timestamp]
            return None
        return entry

    def __len__(self) -> int:
        self._expire(time.time())
        return len(self._entries)


class SessionRegistry:
    """Live MCP sessions per agent id.

    A session joins at ``initialize`` (its agent id from the
    ``X-Signal-Agent-Id`` header, falling back to the MCP session id) and
    leaves when its delivery fails — a closed stream is how a disconnected
    HTTP session announces itself. Several live sessions MAY share an agent
    id; all of them receive that agent's dispatches.
    """

    def __init__(self) -> None:
        # agent id → {session id: session}, insertion-ordered for
        # deterministic delivery.
        self._by_agent: OrderedDict[str, OrderedDict[str, object]] = OrderedDict()
        # session id → agent id, for O(1) removal.
        self._by_session: dict[str, str] = {}

    def register(self, agent_id: str, session_id: str, session: object) -> None:
        self._by_agent.setdefault(agent_id, OrderedDict())[session_id] = session
        self._by_session[session_id] = agent_id
        logger.info(f"Session registered: agent={agent_id!r} session={session_id!r}")

    def unregister(self, session_id: str) -> None:
        """Remove a session by its MCP session id; a no-op when absent."""
        agent_id = self._by_session.pop(session_id, None)
        if agent_id is None:
            return
        sessions = self._by_agent.get(agent_id)
        if sessions is not None:
            sessions.pop(session_id, None)
            if not sessions:
                del self._by_agent[agent_id]
        logger.info(f"Session unregistered: agent={agent_id!r} session={session_id!r}")

    def sessions_for(self, agent_id: str) -> list[tuple[str, object]]:
        """Live (session id, session) pairs for ``agent_id``; empty when offline."""
        return list((self._by_agent.get(agent_id) or {}).items())

    def agent_for_session(self, session_id: str) -> str | None:
        """The agent id a session registered under, or ``None``."""
        return self._by_session.get(session_id)

    def all_sessions(self) -> list[tuple[str, str, object]]:
        """Every live (agent id, session id, session) triple across all agents."""
        return [
            (agent_id, session_id, session)
            for agent_id, sessions in self._by_agent.items()
            for session_id, session in sessions.items()
        ]

    def has_live(self, agent_id: str) -> bool:
        return bool(self._by_agent.get(agent_id))


def resolve_recipients(
    target_timestamp: int | None,
    target_author: str | None,
    account: str,
    routes: RouteTable,
    sessions: SessionRegistry,
    default_agent: str | None,
) -> Dispatch:
    """Decide who receives one inbound reply or reaction (the dispatch table).

    ``target_timestamp``/``target_author`` are the quote's ``id``/``author``
    for a reply and ``reaction.targetSentTimestamp``/``targetAuthor`` for a
    reaction — this function is deliberately content-agnostic. Pure: it
    reads the two registries and returns a :class:`Dispatch`; the caller
    owns delivery and receipt behavior.
    """
    if target_timestamp and target_author and target_author == account:
        entry = routes.lookup(target_timestamp)
        if entry is not None:
            recipients = [
                (entry.agent_id, session_id, session)
                for session_id, session in sessions.sessions_for(entry.agent_id)
            ]
            if recipients:
                return Dispatch(
                    recipients=recipients,
                    route_status="routed",
                    routed_agent=entry.agent_id,
                )
            # The originating agent has no live session: fall through to the
            # unrouted rules, disclosing who the reply was for.
            dispatch = _resolve_unrouted(sessions, default_agent)
            return Dispatch(
                recipients=dispatch.recipients,
                route_status="agent_offline",
                routed_agent=entry.agent_id,
            )
        dispatch = _resolve_unrouted(sessions, default_agent)
        return Dispatch(
            recipients=dispatch.recipients,
            route_status="unknown",
            routed_agent=None,
        )
    return _resolve_unrouted(sessions, default_agent)


def _resolve_unrouted(sessions: SessionRegistry, default_agent: str | None) -> Dispatch:
    """Unrouted-traffic rules: the default agent, else every live session."""
    if default_agent:
        recipients = [
            (default_agent, session_id, session)
            for session_id, session in sessions.sessions_for(default_agent)
        ]
        if recipients:
            return Dispatch(recipients=recipients, route_status="unrouted")
    return Dispatch(
        recipients=[
            (agent_id, session_id, session)
            for agent_id, session_id, session in sessions.all_sessions()
        ],
        route_status="unrouted",
    )


def iter_route_statuses() -> Iterator[str]:
    """The documented route_status values, for tests and docs."""
    yield from ("routed", "agent_offline", "unknown", "unrouted")


# Process-wide routing state (SPEC-0002). Built lazily so the TTL/cap come
# from the parsed config rather than import-time defaults. Both are mutated
# only from the server event loop (forwarder task + request handlers), so no
# locking; a forked or threaded runner must not share them across threads.
_route_table: RouteTable | None = None
_session_registry = SessionRegistry()


def get_route_table() -> RouteTable:
    """The process-wide route table, built from config on first use."""
    global _route_table
    if _route_table is None:
        from signal_mcp.config import config

        _route_table = RouteTable(
            ttl_seconds=config.route_ttl, max_entries=config.route_max_entries
        )
    return _route_table


def reset_routing_state() -> None:
    """Drop the route table and registry — for tests and restarts."""
    global _route_table
    _route_table = None
    global _session_registry
    _session_registry = SessionRegistry()


def get_session_registry() -> SessionRegistry:
    """The process-wide session registry."""
    return _session_registry
