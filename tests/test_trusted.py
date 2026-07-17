import asyncio

import pytest

from signal_mcp import tools
from signal_mcp.config import _load_trusted_recipients, config
from signal_mcp.rpc import UntrustedRecipientError
from signal_mcp.tools import (
    _ensure_trusted,
    _ensure_trusted_group,
    send_message_to_group,
    send_message_to_user,
    send_reaction_to_group,
    send_reaction_to_user,
)

ALICE = "+15555550101"
MALLORY = "+15555550199"
GROUP = "GID=="
GROUP_NAME = "#talk-homelab"


def _fake_resolve(groups):
    """Build a _resolve_group replacement backed by a fixed group list."""

    async def resolve(name_or_id):
        for g in groups:
            if g.get("id") == name_or_id or g.get("name") == name_or_id:
                return g
        return None

    return resolve


def test_ensure_trusted_allows_everything_when_unconfigured(monkeypatch):
    """An empty allowlist disables enforcement (opt-in security)."""
    monkeypatch.setattr(config, "trusted_recipients", frozenset())
    # Should not raise for any recipient.
    _ensure_trusted(MALLORY)


def test_ensure_trusted_allows_listed_recipient(monkeypatch):
    monkeypatch.setattr(config, "trusted_recipients", frozenset({ALICE}))
    _ensure_trusted(ALICE)


def test_ensure_trusted_blocks_unlisted_recipient(monkeypatch):
    monkeypatch.setattr(config, "trusted_recipients", frozenset({ALICE}))
    with pytest.raises(UntrustedRecipientError):
        _ensure_trusted(MALLORY)


def test_ensure_trusted_normalizes_whitespace(monkeypatch):
    monkeypatch.setattr(config, "trusted_recipients", frozenset({ALICE}))
    # A recipient that only differs by surrounding whitespace is still trusted.
    _ensure_trusted(f"  {ALICE}  ")


def test_load_trusted_recipients_merges_cli_and_env(monkeypatch):
    monkeypatch.setenv("SIGNAL_MCP_TRUSTED_RECIPIENTS", f" {MALLORY} , , ")
    result = _load_trusted_recipients([ALICE, ""])
    assert result == frozenset({ALICE, MALLORY})


def test_load_trusted_recipients_empty_without_config(monkeypatch):
    monkeypatch.delenv("SIGNAL_MCP_TRUSTED_RECIPIENTS", raising=False)
    assert _load_trusted_recipients([]) == frozenset()


def test_send_message_to_user_blocks_untrusted(monkeypatch):
    """An untrusted recipient is rejected before signal-cli is ever invoked."""
    called = False

    async def fake_send(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(tools, "_send_message", fake_send)
    monkeypatch.setattr(config, "trusted_recipients", frozenset({ALICE}))

    with pytest.raises(UntrustedRecipientError) as excinfo:
        asyncio.run(send_message_to_user("hi", MALLORY))

    assert MALLORY in str(excinfo.value)
    assert called is False  # never asked the daemon to send


def test_send_message_to_user_allows_trusted(monkeypatch):
    sent = {}

    async def fake_send(message, target, is_group=False, attachments=None):
        sent["target"] = target

    monkeypatch.setattr(tools, "_send_message", fake_send)
    monkeypatch.setattr(config, "trusted_recipients", frozenset({ALICE}))

    result = asyncio.run(send_message_to_user("hi", ALICE))

    assert result == {"message": "Message sent successfully"}
    assert sent["target"] == ALICE


def test_send_reaction_to_user_blocks_untrusted(monkeypatch):
    called = False

    async def fake_reaction(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(tools, "_send_reaction", fake_reaction)
    monkeypatch.setattr(config, "trusted_recipients", frozenset({ALICE}))

    with pytest.raises(UntrustedRecipientError):
        asyncio.run(
            send_reaction_to_user("\U0001f44d", MALLORY, MALLORY, 1782554453770)
        )

    assert called is False


def test_send_message_to_group_blocks_untrusted(monkeypatch):
    """An untrusted group is rejected before any send, even after resolution."""
    sent = False

    async def fake_send(*args, **kwargs):
        nonlocal sent
        sent = True

    monkeypatch.setattr(
        tools,
        "_resolve_group",
        _fake_resolve([{"id": "OTHER_GID==", "name": "#other"}]),
    )
    monkeypatch.setattr(tools, "_send_message", fake_send)
    monkeypatch.setattr(config, "trusted_recipients", frozenset({GROUP}))

    with pytest.raises(UntrustedRecipientError):
        asyncio.run(send_message_to_group("hi", "OTHER_GID=="))

    assert sent is False  # never asked the daemon to send


def test_send_message_to_group_blocks_unresolvable_untrusted(monkeypatch):
    """With a non-empty allowlist, an unknown group can't be verified — reject."""
    monkeypatch.setattr(tools, "_resolve_group", _fake_resolve([]))
    monkeypatch.setattr(config, "trusted_recipients", frozenset({GROUP}))

    with pytest.raises(UntrustedRecipientError):
        asyncio.run(_ensure_trusted_group("mystery-group"))


def test_send_message_to_group_allows_trusted(monkeypatch):
    sent = {}

    async def fake_send(message, target, is_group=False, attachments=None):
        sent["target"] = target
        sent["is_group"] = is_group

    monkeypatch.setattr(
        tools,
        "_resolve_group",
        _fake_resolve([{"id": GROUP, "name": GROUP_NAME}]),
    )
    monkeypatch.setattr(tools, "_send_message", fake_send)
    monkeypatch.setattr(config, "trusted_recipients", frozenset({GROUP}))

    result = asyncio.run(send_message_to_group("hi", GROUP))

    assert result == {"message": "Message sent successfully"}
    assert sent == {"target": GROUP, "is_group": True}


def test_group_allowlisted_by_name_send_by_id(monkeypatch):
    """The allowlist holds the display name; the caller passes the id."""
    monkeypatch.setattr(
        tools,
        "_resolve_group",
        _fake_resolve([{"id": GROUP, "name": GROUP_NAME}]),
    )
    monkeypatch.setattr(config, "trusted_recipients", frozenset({GROUP_NAME}))

    assert asyncio.run(_ensure_trusted_group(GROUP)) == GROUP


def test_group_allowlisted_by_id_send_by_name(monkeypatch):
    """The allowlist holds the id; the caller passes the display name."""
    sent = {}

    async def fake_send(message, target, is_group=False, attachments=None):
        sent["target"] = target

    monkeypatch.setattr(
        tools,
        "_resolve_group",
        _fake_resolve([{"id": GROUP, "name": GROUP_NAME}]),
    )
    monkeypatch.setattr(tools, "_send_message", fake_send)
    monkeypatch.setattr(config, "trusted_recipients", frozenset({GROUP}))

    result = asyncio.run(send_message_to_group("hi", GROUP_NAME))

    assert result == {"message": "Message sent successfully"}
    assert sent["target"] == GROUP  # resolved to the internal id


def test_send_reaction_to_group_blocks_untrusted(monkeypatch):
    called = False

    async def fake_reaction(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        tools,
        "_resolve_group",
        _fake_resolve([{"id": "OTHER_GID==", "name": "#other"}]),
    )
    monkeypatch.setattr(tools, "_send_reaction", fake_reaction)
    monkeypatch.setattr(config, "trusted_recipients", frozenset({GROUP}))

    with pytest.raises(UntrustedRecipientError):
        asyncio.run(
            send_reaction_to_group("\U0001f44d", "OTHER_GID==", MALLORY, 1782554453770)
        )

    assert called is False
