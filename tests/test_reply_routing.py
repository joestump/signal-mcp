"""Tests for SPEC-0002: quote parsing, reply meta, and channel dispatch."""

import asyncio
from unittest.mock import patch


from signal_mcp import routing, rpc
from signal_mcp.channel import _base_meta, _forward_channel_messages
from signal_mcp.config import config
from signal_mcp.parse import MessageResponse, Quote, _envelope_to_response

from tests.test_channel import FakeClient, FakeWriteStream

ACCOUNT = "+15550000000"


def _envelope(content: dict, source: str = "+15550001111") -> dict:
    return {"envelope": {"source": source, "timestamp": 1, **content}}


def _parse(payload: dict) -> "MessageResponse":
    response = _envelope_to_response(payload)
    assert response is not None
    return response


def _parse_quote_of(payload: dict) -> "Quote":
    quote = _parse(payload).quote
    assert quote is not None
    return quote


class TestQuoteParsing:
    def test_reply_carries_quote(self):
        response = _parse(
            _envelope(
                {
                    "dataMessage": {
                        "message": "yes, roll it out",
                        "timestamp": 1800,
                        "quote": {
                            "id": 1700,
                            "author": "+15551230000",
                            "text": "deploy finished",
                        },
                    }
                }
            )
        )
        assert response is not None
        assert response.quote is not None
        assert response.quote.timestamp == 1700
        assert response.quote is not None
        assert response.quote.author == "+15551230000"
        assert response.quote.text == "deploy finished"

    def test_author_falls_back_to_author_number(self):
        response = _parse(
            _envelope(
                {
                    "dataMessage": {
                        "message": "ok",
                        "quote": {"id": 1700, "authorNumber": "+15551230000"},
                    }
                }
            )
        )
        assert response.quote is not None
        assert response.quote.author == "+15551230000"

    def test_plain_message_has_no_quote(self):
        response = _parse(_envelope({"dataMessage": {"message": "hi", "timestamp": 2}}))
        assert response.quote is None
        assert response.message == "hi"

    def test_oversized_quoted_text_truncated_at_boundary(self):
        response = _parse(
            _envelope(
                {
                    "dataMessage": {
                        "message": "ok",
                        "quote": {"id": 1700, "author": "+1", "text": "é" * 300},
                    }
                }
            )
        )
        assert response.quote is not None
        text = response.quote.text or ""
        assert len(text.encode()) <= 256
        text.encode("utf-8")  # no partial character

    def test_non_actionable_envelope_still_dropped(self):
        assert (
            _envelope_to_response(
                _envelope({"dataMessage": {"quote": {"id": 1, "text": "x"}}})
            )
            is None
        )

    def test_reaction_envelope_carries_quote(self):
        response = _parse(
            _envelope(
                {
                    "dataMessage": {
                        "reaction": {
                            "emoji": "👍",
                            "targetAuthor": ACCOUNT,
                            "targetSentTimestamp": 1700,
                            "isRemove": False,
                        },
                        "quote": {"id": 1700, "author": ACCOUNT, "text": "hi"},
                    }
                }
            )
        )
        assert response.reaction is not None
        assert response.quote is not None
        assert response.quote.timestamp == 1700


class TestReplyMeta:
    def test_reply_notification_is_annotated(self):
        msg = _parse(
            _envelope(
                {
                    "dataMessage": {
                        "message": "yes",
                        "timestamp": 1800,
                        "quote": {
                            "id": 1700,
                            "author": ACCOUNT,
                            "text": "deploy finished",
                        },
                    }
                }
            )
        )
        meta = _base_meta(msg)
        assert meta["in_reply_to_timestamp"] == "1700"
        assert meta["in_reply_to_author"] == ACCOUNT
        assert meta["in_reply_to_text"] == "deploy finished"

    def test_reply_with_empty_quote_text_omits_text_key(self):
        msg = _parse(
            _envelope(
                {
                    "dataMessage": {
                        "message": "yes",
                        "quote": {"id": 1700, "author": ACCOUNT},
                    }
                }
            )
        )
        meta = _base_meta(msg)
        assert meta["in_reply_to_timestamp"] == "1700"
        assert "in_reply_to_text" not in meta

    def test_non_reply_notification_has_no_reply_keys(self):
        msg = _parse(
            _envelope({"dataMessage": {"message": "weather?", "timestamp": 2}})
        )
        meta = _base_meta(msg)
        assert not any(k.startswith("in_reply_to_") for k in meta)
        assert "sender" in meta

    def test_instructions_describe_reply_keys(self):
        from signal_mcp.channel import CHANNEL_INSTRUCTIONS

        assert "in_reply_to_timestamp" in CHANNEL_INSTRUCTIONS
        assert "in_reply_to_author" in CHANNEL_INSTRUCTIONS


def _reply_msg():
    return _envelope_to_response(
        _envelope(
            {
                "dataMessage": {
                    "message": "yes, roll it out",
                    "timestamp": 1800,
                    "quote": {
                        "id": 1700,
                        "author": ACCOUNT,
                        "text": "deploy finished",
                    },
                }
            }
        )
    )


def _plain_msg():
    return _envelope_to_response(
        _envelope({"dataMessage": {"message": "what's the weather?", "timestamp": 2}})
    )


class _RecordingSession:
    def __init__(self) -> None:
        self.notifications: list = []
        self.fail = False

    async def send_notification(self, notification) -> None:
        if self.fail:
            raise ConnectionError("stream closed")
        self.notifications.append(notification)


def _setup_routing(monkeypatch, account=ACCOUNT, default_agent=""):
    monkeypatch.setattr(config, "account", account)
    monkeypatch.setattr(config, "channel_mode", True)
    monkeypatch.setattr(config, "transport", "http")
    monkeypatch.setattr(config, "default_agent", default_agent)
    monkeypatch.setattr(config, "trusted_senders", frozenset({"+15550001111"}))
    monkeypatch.setattr(config, "operator", "+15550001111")
    routing.reset_routing_state()
    return routing.get_route_table(), routing.get_session_registry()


def _run_forwarder(monkeypatch, messages):
    stream = FakeWriteStream()
    client = FakeClient(list(messages))

    async def _run():
        task = asyncio.create_task(_forward_channel_messages(stream))
        while not client._messages:
            await asyncio.sleep(0)
            if task.done():
                break
        # Let the forwarder drain the queue and dispatch.
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with patch.object(rpc, "client", client):
        asyncio.run(_run())
    return client


class TestChannelDispatch:
    def test_reply_reaches_only_originating_agent(self, monkeypatch):
        routes, sessions = _setup_routing(monkeypatch)
        routes.record(1700, "agent-a", "k", "deploy finished")
        a, b = _RecordingSession(), _RecordingSession()
        sessions.register("agent-a", "s1", a)
        sessions.register("agent-b", "s2", b)

        client = _run_forwarder(monkeypatch, [_reply_msg()])
        assert len(a.notifications) == 1
        assert b.notifications == []
        params = a.notifications[0].params.meta
        assert params["route_status"] == "routed"
        assert params["routed_agent"] == "agent-a"
        assert params["in_reply_to_timestamp"] == "1700"
        # Single read receipt despite one recipient.
        assert (
            client.calls.count(
                (
                    "sendReceipt",
                    {
                        "recipient": ["+15550001111"],
                        "targetTimestamp": 1800,
                        "type": "read",
                    },
                )
            )
            == 1
        )

    def test_plain_message_goes_to_default_agent_only(self, monkeypatch):
        routes, sessions = _setup_routing(monkeypatch, default_agent="default")
        a, b = _RecordingSession(), _RecordingSession()
        sessions.register("default", "s1", a)
        sessions.register("other", "s2", b)

        _run_forwarder(monkeypatch, [_plain_msg()])
        assert len(a.notifications) == 1
        assert b.notifications == []
        meta = a.notifications[0].params.meta
        assert meta["route_status"] == "unrouted"
        assert "routed_agent" not in meta

    def test_no_default_fans_out(self, monkeypatch):
        routes, sessions = _setup_routing(monkeypatch)
        a, b = _RecordingSession(), _RecordingSession()
        sessions.register("agent-a", "s1", a)
        sessions.register("agent-b", "s2", b)

        _run_forwarder(monkeypatch, [_plain_msg()])
        assert len(a.notifications) == 1
        assert len(b.notifications) == 1

    def test_unknown_reply_carries_context(self, monkeypatch):
        routes, sessions = _setup_routing(monkeypatch, default_agent="default")
        a = _RecordingSession()
        sessions.register("default", "s1", a)

        _run_forwarder(monkeypatch, [_reply_msg()])
        meta = a.notifications[0].params.meta
        assert meta["route_status"] == "unknown"
        assert meta["in_reply_to_timestamp"] == "1700"
        assert meta["in_reply_to_text"] == "deploy finished"

    def test_offline_agent_reply_falls_back_disclosing_agent(self, monkeypatch):
        routes, sessions = _setup_routing(monkeypatch, default_agent="default")
        routes.record(1700, "gone", "k", "deploy finished")
        a = _RecordingSession()
        sessions.register("default", "s1", a)

        _run_forwarder(monkeypatch, [_reply_msg()])
        meta = a.notifications[0].params.meta
        assert meta["route_status"] == "agent_offline"
        assert meta["routed_agent"] == "gone"

    def test_prefix_filter_applies_to_routed_replies(self, monkeypatch):
        routes, sessions = _setup_routing(monkeypatch)
        routes.record(1700, "agent-a", "k", "deploy finished")
        a = _RecordingSession()
        sessions.register("agent-a", "s1", a)
        monkeypatch.setattr(config, "prefix", "cc")
        from signal_mcp.parse import (
            _envelope_to_response as parse,
        )

        msg = parse(
            _envelope(
                {
                    "dataMessage": {
                        "message": "no prefix here",
                        "timestamp": 1800,
                        "quote": {"id": 1700, "author": ACCOUNT, "text": "x"},
                    }
                }
            )
        )
        _run_forwarder(monkeypatch, [msg])
        assert a.notifications == []

    def test_no_receipt_when_no_session_received(self, monkeypatch):
        _setup_routing(monkeypatch)
        # No sessions registered at all: nothing delivered, no receipt.
        client = _run_forwarder(monkeypatch, [_plain_msg()])
        assert not any(m == "sendReceipt" for m, _ in client.calls)

    def test_delivery_failure_isolated_and_session_dropped(self, monkeypatch):
        routes, sessions = _setup_routing(monkeypatch)
        import signal_mcp.routing as _r

        _r.reset_routing_state()
        routes, sessions = _r.get_route_table(), _r.get_session_registry()
        routes.record(1700, "agent-a", "k", "deploy finished")
        good, bad = _RecordingSession(), _RecordingSession()
        bad.fail = True
        sessions.register("agent-a", "s-good", good)
        sessions.register("agent-a", "s-bad", bad)

        _run_forwarder(monkeypatch, [_reply_msg()])
        # The good session still received it; the failed one was dropped
        # from the registry while the agent itself stays live.
        assert len(good.notifications) == 1
        assert sessions.has_live("agent-a")
        assert [sid for sid, _ in sessions.sessions_for("agent-a")] == ["s-good"]

    def test_reaction_routed_like_reply(self, monkeypatch):
        routes, sessions = _setup_routing(monkeypatch)
        routes.record(1700, "agent-a", "k", "deploy finished")
        a, b = _RecordingSession(), _RecordingSession()
        sessions.register("agent-a", "s1", a)
        sessions.register("agent-b", "s2", b)

        from signal_mcp.parse import (
            _envelope_to_response as parse,
        )

        reaction = parse(
            _envelope(
                {
                    "dataMessage": {
                        "reaction": {
                            "emoji": "👍",
                            "targetAuthor": ACCOUNT,
                            "targetSentTimestamp": 1700,
                            "isRemove": False,
                        }
                    }
                }
            )
        )
        _run_forwarder(monkeypatch, [reaction])
        assert len(a.notifications) == 1
        assert b.notifications == []
        meta = a.notifications[0].params.meta
        assert meta["route_status"] == "routed"
        assert meta["reaction_target_timestamp"] == "1700"

    def test_stdio_mode_has_no_route_keys(self, monkeypatch):
        monkeypatch.setattr(config, "account", ACCOUNT)
        monkeypatch.setattr(config, "channel_mode", True)
        monkeypatch.setattr(config, "transport", "stdio")
        monkeypatch.setattr(config, "trusted_senders", frozenset({"+15550001111"}))
        routing.reset_routing_state()

        stream = FakeWriteStream()
        client = FakeClient([_reply_msg()])

        async def _run():
            task = asyncio.create_task(_forward_channel_messages(stream))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with patch.object(rpc, "client", client):
            asyncio.run(_run())
        assert len(stream.sent) == 1
        notif = stream.sent[0].message.root
        assert notif.method == "notifications/claude/channel"
        assert "route_status" not in notif.params["meta"]
        assert notif.params["meta"]["in_reply_to_timestamp"] == "1700"
