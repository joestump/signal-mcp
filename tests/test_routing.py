"""Tests for SPEC-0002 reply routing: route table, session registry, dispatch."""

import pytest

from signal_mcp.routing import (
    Dispatch,
    RouteTable,
    SessionRegistry,
    normalize_agent_id,
    resolve_recipients,
    truncate_utf8,
)

ACCOUNT = "+15550000000"


def _routes_with(ts: int, agent: str) -> RouteTable:
    routes = RouteTable()
    routes.record(ts, agent, "conv", "hello")
    return routes


def _sessions(*pairs: tuple[str, str, object]) -> SessionRegistry:
    registry = SessionRegistry()
    for agent_id, session_id, session in pairs:
        registry.register(agent_id, session_id, session)
    return registry


class TestTruncateUtf8:
    def test_short_text_unchanged(self):
        assert truncate_utf8("deploy finished") == "deploy finished"

    def test_ascii_truncated_at_limit(self):
        assert len(truncate_utf8("a" * 400).encode()) == 256

    def test_multibyte_truncated_at_character_boundary(self):
        text = "é" * 300  # 2 bytes each; 256 bytes cuts mid-character
        result = truncate_utf8(text)
        assert len(result.encode()) <= 256
        result.encode("utf-8")  # would raise on a partial character


class TestNormalizeAgentId:
    def test_trims_whitespace(self):
        assert normalize_agent_id("  deploy-bot  ") == "deploy-bot"

    def test_blank_is_none(self):
        assert normalize_agent_id("   ") is None
        assert normalize_agent_id("") is None
        assert normalize_agent_id(None) is None

    def test_capped_at_128_bytes(self):
        assert len((normalize_agent_id("x" * 500) or "").encode()) == 128


class TestRouteTable:
    def test_record_and_lookup(self):
        routes = RouteTable()
        routes.record(1700, "agent-a", "conv-key", "deploy finished")
        entry = routes.lookup(1700)
        assert entry is not None
        assert entry.agent_id == "agent-a"
        assert entry.conversation_key == "conv-key"
        assert entry.preview == "deploy finished"

    def test_lookup_miss(self):
        assert RouteTable().lookup(1700) is None

    def test_expired_entry_forgotten(self, monkeypatch):
        import time

        routes = RouteTable(ttl_seconds=10)
        real_time = time.time
        routes.record(1700, "agent-a", "k", "")
        assert routes.lookup(1700) is not None
        # Advance the clock past the TTL; the lazy expiry must forget it.
        monkeypatch.setattr(time, "time", lambda: real_time() + 11)
        assert routes.lookup(1700) is None
        assert len(routes) == 0

    def test_full_table_evicts_oldest(self):
        routes = RouteTable(max_entries=2)
        routes.record(1, "a", "k", "")
        routes.record(2, "a", "k", "")
        routes.record(3, "a", "k", "")
        assert routes.lookup(1) is None
        assert routes.lookup(2) is not None
        assert routes.lookup(3) is not None

    def test_rerecord_keeps_newest(self):
        routes = RouteTable()
        routes.record(1, "a", "k", "")
        routes.record(1, "b", "k", "")
        entry = routes.lookup(1)
        assert entry is not None
        assert entry.agent_id == "b"
        assert len(routes) == 1

    def test_invalid_bounds_rejected(self):
        with pytest.raises(ValueError):
            RouteTable(ttl_seconds=0)
        with pytest.raises(ValueError):
            RouteTable(max_entries=0)

    def test_preview_capped_at_256_bytes(self):
        routes = RouteTable()
        routes.record(1, "a", "k", "x" * 500)
        entry = routes.lookup(1)
        assert entry is not None
        assert len(entry.preview.encode()) == 256


class TestSessionRegistry:
    def test_register_and_lookup(self):
        sessions = SessionRegistry()
        sessions.register("agent-a", "s1", object())
        assert [sid for sid, _ in sessions.sessions_for("agent-a")] == ["s1"]
        assert sessions.has_live("agent-a")

    def test_offline_agent(self):
        assert not SessionRegistry().has_live("agent-a")

    def test_unregister_removes(self):
        sessions = SessionRegistry()
        sessions.register("agent-a", "s1", object())
        sessions.unregister("s1")
        assert sessions.sessions_for("agent-a") == []
        assert not sessions.has_live("agent-a")

    def test_unregister_unknown_is_noop(self):
        SessionRegistry().unregister("nope")

    def test_multiple_sessions_share_agent_id(self):
        sessions = SessionRegistry()
        sessions.register("agent-a", "s1", "one")
        sessions.register("agent-a", "s2", "two")
        assert len(sessions.sessions_for("agent-a")) == 2
        sessions.unregister("s1")
        assert len(sessions.sessions_for("agent-a")) == 1

    def test_all_sessions(self):
        sessions = SessionRegistry()
        sessions.register("a", "s1", "one")
        sessions.register("b", "s2", "two")
        assert len(sessions.all_sessions()) == 2


class _FakeSession:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.notifications: list = []

    async def send_notification(self, notification) -> None:
        if self.fail:
            raise ConnectionError("stream closed")
        self.notifications.append(notification)


class TestResolveRecipients:
    def test_routed_to_originating_agent_only(self):
        a, b = _FakeSession(), _FakeSession()
        sessions = _sessions(("agent-a", "s1", a), ("agent-b", "s2", b))
        dispatch = resolve_recipients(
            1700, ACCOUNT, ACCOUNT, _routes_with(1700, "agent-a"), sessions, None
        )
        assert dispatch.route_status == "routed"
        assert dispatch.routed_agent == "agent-a"
        assert [(sid, s) for _aid, sid, s in dispatch.recipients] == [("s1", a)]

    def test_originating_agent_offline_falls_back(self):
        b = _FakeSession()
        sessions = _sessions(("agent-b", "s2", b))
        dispatch = resolve_recipients(
            1700, ACCOUNT, ACCOUNT, _routes_with(1700, "agent-a"), sessions, None
        )
        assert dispatch.route_status == "agent_offline"
        assert dispatch.routed_agent == "agent-a"
        assert b in [s for _, _, s in dispatch.recipients]

    def test_offline_with_default_goes_to_default(self):
        default = _FakeSession()
        other = _FakeSession()
        sessions = _sessions(("default", "s1", default), ("other", "s2", other))
        dispatch = resolve_recipients(
            1700, ACCOUNT, ACCOUNT, _routes_with(1700, "agent-a"), sessions, "default"
        )
        assert dispatch.route_status == "agent_offline"
        assert [(sid, s) for _aid, sid, s in dispatch.recipients] == [("s1", default)]

    def test_unknown_timestamp_falls_back(self):
        b = _FakeSession()
        sessions = _sessions(("agent-b", "s2", b))
        dispatch = resolve_recipients(
            9999, ACCOUNT, ACCOUNT, _routes_with(1700, "agent-a"), sessions, None
        )
        assert dispatch.route_status == "unknown"
        assert dispatch.routed_agent is None
        assert b in [s for _, _, s in dispatch.recipients]

    def test_reply_to_other_author_is_unrouted(self):
        # quote author is someone else's number (not the account) — even with
        # the timestamp in the table, this is unrouted traffic.
        b = _FakeSession()
        sessions = _sessions(("agent-b", "s2", b))
        dispatch = resolve_recipients(
            1700, "+15551112222", ACCOUNT, _routes_with(1700, "agent-a"), sessions, None
        )
        assert dispatch.route_status == "unrouted"

    def test_non_reply_is_unrouted(self):
        b = _FakeSession()
        sessions = _sessions(("agent-b", "s2", b))
        dispatch = resolve_recipients(
            None, None, ACCOUNT, _routes_with(1700, "agent-a"), sessions, None
        )
        assert dispatch.route_status == "unrouted"
        assert b in [s for _, _, s in dispatch.recipients]

    def test_unrouted_goes_to_default_agent_only(self):
        default = _FakeSession()
        other = _FakeSession()
        sessions = _sessions(("default", "s1", default), ("other", "s2", other))
        dispatch = resolve_recipients(
            None, None, ACCOUNT, RouteTable(), sessions, "default"
        )
        assert dispatch.route_status == "unrouted"
        assert [s for _, _, s in dispatch.recipients] == [default]

    def test_unrouted_default_offline_fans_out(self):
        other = _FakeSession()
        sessions = _sessions(("other", "s2", other))
        dispatch = resolve_recipients(
            None, None, ACCOUNT, RouteTable(), sessions, "default"
        )
        assert dispatch.route_status == "unrouted"
        assert [s for _, _, s in dispatch.recipients] == [other]

    def test_no_recipients_at_all(self):
        dispatch = resolve_recipients(
            None, None, ACCOUNT, RouteTable(), SessionRegistry(), None
        )
        assert dispatch == Dispatch(recipients=[], route_status="unrouted")
