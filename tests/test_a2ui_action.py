"""Tests for the a2ui_action round-trip tool.

Covers SPEC-0001 REQ "Interactive Actions": react and reply dispatch
correctly from button context, the trusted-recipients allowlist rejects
untrusted targets exactly as the direct tools do, and hosts that never
invoke actions are unaffected.
"""

import pytest

from signal_mcp import rpc, tools
from signal_mcp.config import config
from signal_mcp.rpc import SignalCLIError, SignalError, UntrustedRecipientError
from signal_mcp.tools import a2ui_action
from tests.helpers import run_tool

ALICE = "+15555550101"
MALLORY = "+15555550199"
GROUP = "2Zm5AIE6Qo6VBw4dDQ4aGVd2MvoQ0Q8A3Q+7vw7qOrI==="
GROUP_NAME = "#talk-homelab"
TS = 1744185565466

GROUPS = [{"id": GROUP, "name": GROUP_NAME}]


class FakeClient:
    """Stand-in for SignalRpcClient that records JSON-RPC calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        return {"timestamp": TS}


def _fake_resolve(groups):
    """Build a _resolve_group replacement backed by a fixed group list."""

    async def resolve(name_or_id):
        for g in groups:
            if g.get("id") == name_or_id or g.get("name") == name_or_id:
                return g
        return None

    return resolve


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(rpc, "client", fake)
    monkeypatch.setattr(tools, "_resolve_group", _fake_resolve(GROUPS))
    monkeypatch.setattr(config, "trusted_recipients", frozenset())
    return fake


@pytest.fixture
def allowlist_alice(monkeypatch):
    monkeypatch.setattr(config, "trusted_recipients", frozenset({ALICE}))


# ---------------------------------------------------------------------------
# react
# ---------------------------------------------------------------------------


def test_react_dm_dispatches_send_reaction(client):
    result = run_tool(
        a2ui_action(
            "react",
            {
                "conversation_id": ALICE,
                "target_author": ALICE,
                "target_timestamp": TS,
                "emoji": "👍",
            },
        )
    )
    assert result == {"message": "Reaction sent successfully"}
    method, params = client.calls[-1]
    assert method == "sendReaction"
    assert params["recipient"] == [ALICE]
    assert "groupId" not in params
    assert params["targetAuthor"] == ALICE
    assert params["targetTimestamp"] == TS
    assert params["emoji"] == "👍"
    assert params["remove"] is False


def test_react_dm_remove_flag_passes_through(client):
    run_tool(
        a2ui_action(
            "react",
            {
                "conversation_id": ALICE,
                "target_author": ALICE,
                "target_timestamp": TS,
                "emoji": "👍",
                "remove": True,
            },
        )
    )
    _, params = client.calls[-1]
    assert params["remove"] is True


def test_react_group_dispatches_with_group_id(client):
    run_tool(
        a2ui_action(
            "react",
            {
                "conversation_id": GROUP,
                "target_author": ALICE,
                "target_timestamp": TS,
                "emoji": "🎉",
            },
        )
    )
    method, params = client.calls[-1]
    assert method == "sendReaction"
    assert params["groupId"] == GROUP
    assert "recipient" not in params


def test_react_group_display_name_resolves_to_id(client):
    run_tool(
        a2ui_action(
            "react",
            {
                "conversation_id": GROUP_NAME,
                "target_author": ALICE,
                "target_timestamp": TS,
                "emoji": "🎉",
            },
        )
    )
    _, params = client.calls[-1]
    assert params["groupId"] == GROUP


def test_react_numeric_string_timestamp_coerced(client):
    run_tool(
        a2ui_action(
            "react",
            {
                "conversation_id": ALICE,
                "target_author": ALICE,
                "target_timestamp": str(TS),
                "emoji": "👍",
            },
        )
    )
    _, params = client.calls[-1]
    assert params["targetTimestamp"] == TS
    assert isinstance(params["targetTimestamp"], int)


def test_react_without_emoji_defaults_to_thumbs_up(client):
    run_tool(
        a2ui_action(
            "react",
            {
                "conversation_id": ALICE,
                "target_author": ALICE,
                "target_timestamp": TS,
            },
        )
    )
    method, params = client.calls[-1]
    assert method == "sendReaction"
    assert params["emoji"] == "\U0001f44d"


def test_react_rejects_non_string_emoji(client):
    with pytest.raises(SignalError):
        run_tool(
            a2ui_action(
                "react",
                {
                    "conversation_id": ALICE,
                    "target_author": ALICE,
                    "target_timestamp": TS,
                    "emoji": 7,
                },
            )
        )
    assert client.calls == []


@pytest.mark.parametrize("bad", [None, "not-a-number", 1.5, True])
def test_react_rejects_bad_timestamp(client, bad):
    with pytest.raises(SignalError):
        run_tool(
            a2ui_action(
                "react",
                {
                    "conversation_id": ALICE,
                    "target_author": ALICE,
                    "target_timestamp": bad,
                    "emoji": "👍",
                },
            )
        )
    assert client.calls == []


def test_react_requires_target_author(client):
    with pytest.raises(SignalError):
        run_tool(
            a2ui_action(
                "react",
                {
                    "conversation_id": ALICE,
                    "target_timestamp": TS,
                    "emoji": "👍",
                },
            )
        )
    assert client.calls == []


# ---------------------------------------------------------------------------
# reply
# ---------------------------------------------------------------------------


def test_reply_dm_dispatches_send_message(client):
    result = run_tool(
        a2ui_action("reply", {"conversation_id": ALICE, "message": "hi back"})
    )
    assert result == {"message": "Message sent successfully"}
    method, params = client.calls[-1]
    assert method == "send"
    assert params["recipient"] == [ALICE]
    assert "groupId" not in params
    assert params["message"] == "hi back"


def test_reply_group_uses_resolved_group_id(client):
    run_tool(a2ui_action("reply", {"conversation_id": GROUP, "message": "hi group"}))
    method, params = client.calls[-1]
    assert method == "send"
    assert params["groupId"] == GROUP
    assert "recipient" not in params


def test_reply_without_message_sends_empty_body(client):
    run_tool(a2ui_action("reply", {"conversation_id": ALICE}))
    _, params = client.calls[-1]
    assert params["message"] == ""


def test_reply_rejects_non_string_message(client):
    with pytest.raises(SignalError):
        run_tool(a2ui_action("reply", {"conversation_id": ALICE, "message": 42}))
    assert client.calls == []


def test_action_without_conversation_id_rejected(client):
    with pytest.raises(SignalError):
        run_tool(a2ui_action("reply", {"message": "hi"}))
    assert client.calls == []


def test_context_none_rejected(client):
    with pytest.raises(SignalError):
        run_tool(a2ui_action("reply", None))
    assert client.calls == []


def test_unknown_action_rejected(client):
    with pytest.raises(SignalError):
        run_tool(a2ui_action("confirm_booking", {"conversation_id": ALICE}))
    assert client.calls == []


# ---------------------------------------------------------------------------
# Allowlist enforcement (SPEC-0001 scenario "Actions cannot bypass the
# allowlist"): the rejection must match the equivalent direct tool call.
# ---------------------------------------------------------------------------


def test_reply_to_untrusted_user_raises_untrusted_error(client, allowlist_alice):
    with pytest.raises(UntrustedRecipientError):
        run_tool(a2ui_action("reply", {"conversation_id": MALLORY, "message": "x"}))
    assert client.calls == []


def test_react_to_untrusted_user_raises_untrusted_error(client, allowlist_alice):
    with pytest.raises(UntrustedRecipientError):
        run_tool(
            a2ui_action(
                "react",
                {
                    "conversation_id": MALLORY,
                    "target_author": MALLORY,
                    "target_timestamp": TS,
                    "emoji": "👍",
                },
            )
        )
    assert client.calls == []


def test_reply_to_untrusted_group_raises_untrusted_error(client, allowlist_alice):
    with pytest.raises(UntrustedRecipientError):
        run_tool(a2ui_action("reply", {"conversation_id": GROUP, "message": "x"}))
    assert client.calls == []


def test_trusted_user_reply_still_works(client, allowlist_alice):
    run_tool(a2ui_action("reply", {"conversation_id": ALICE, "message": "ok"}))
    assert client.calls[-1][0] == "send"


def test_group_id_never_treated_as_user_when_allowlist_disabled(client):
    """Shape rule: a group key dispatches with groupId even with no allowlist."""
    run_tool(a2ui_action("reply", {"conversation_id": GROUP, "message": "x"}))
    _, params = client.calls[-1]
    assert "recipient" not in params
    assert params["groupId"] == GROUP


def test_unknown_group_with_disabled_allowlist_finds_no_target(client, monkeypatch):
    monkeypatch.setattr(tools, "_resolve_group", _fake_resolve([]))
    with pytest.raises(SignalError):
        run_tool(a2ui_action("reply", {"conversation_id": GROUP, "message": "x"}))


def test_daemon_failure_surfaces_as_error(client, monkeypatch):
    async def failing_call(method, params=None, timeout=30.0):
        raise SignalCLIError("connection refused")

    monkeypatch.setattr(client, "call", failing_call)
    with pytest.raises(SignalCLIError):
        run_tool(a2ui_action("reply", {"conversation_id": ALICE, "message": "x"}))
