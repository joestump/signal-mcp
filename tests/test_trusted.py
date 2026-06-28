import asyncio

from signal_mcp import main
from signal_mcp.main import (
    UntrustedRecipientError,
    _ensure_trusted,
    _load_trusted_recipients,
    send_message_to_user,
    send_reaction_to_user,
)

ALICE = "+15555550101"
MALLORY = "+15555550199"


def test_ensure_trusted_allows_everything_when_unconfigured(monkeypatch):
    """An empty allowlist disables enforcement (opt-in security)."""
    monkeypatch.setattr(main.config, "trusted_recipients", frozenset())
    # Should not raise for any recipient.
    _ensure_trusted(MALLORY)


def test_ensure_trusted_allows_listed_recipient(monkeypatch):
    monkeypatch.setattr(main.config, "trusted_recipients", frozenset({ALICE}))
    _ensure_trusted(ALICE)


def test_ensure_trusted_blocks_unlisted_recipient(monkeypatch):
    monkeypatch.setattr(main.config, "trusted_recipients", frozenset({ALICE}))
    raised = False
    try:
        _ensure_trusted(MALLORY)
    except UntrustedRecipientError:
        raised = True
    assert raised, "expected UntrustedRecipientError for an unlisted recipient"


def test_ensure_trusted_normalizes_whitespace(monkeypatch):
    monkeypatch.setattr(main.config, "trusted_recipients", frozenset({ALICE}))
    # A recipient that only differs by surrounding whitespace is still trusted.
    _ensure_trusted(f"  {ALICE}  ")


def test_load_trusted_recipients_merges_cli_and_env(monkeypatch):
    monkeypatch.setenv("SIGNAL_TRUSTED_RECIPIENTS", f" {MALLORY} , , ")
    result = _load_trusted_recipients([ALICE, ""])
    assert result == frozenset({ALICE, MALLORY})


def test_load_trusted_recipients_empty_without_config(monkeypatch):
    monkeypatch.delenv("SIGNAL_TRUSTED_RECIPIENTS", raising=False)
    assert _load_trusted_recipients([]) == frozenset()


def test_send_message_to_user_blocks_untrusted(monkeypatch):
    """An untrusted recipient is rejected before signal-cli is ever invoked."""
    called = False

    async def fake_send(*args, **kwargs):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(main, "_send_message", fake_send)
    monkeypatch.setattr(main.config, "trusted_recipients", frozenset({ALICE}))

    result = asyncio.run(send_message_to_user("hi", MALLORY))

    assert "error" in result
    assert MALLORY in result["error"]
    assert called is False  # never shelled out


def test_send_message_to_user_allows_trusted(monkeypatch):
    sent = {}

    async def fake_send(message, target, is_group=False):
        sent["target"] = target
        return True

    monkeypatch.setattr(main, "_send_message", fake_send)
    monkeypatch.setattr(main.config, "trusted_recipients", frozenset({ALICE}))

    result = asyncio.run(send_message_to_user("hi", ALICE))

    assert result == {"message": "Message sent successfully"}
    assert sent["target"] == ALICE


def test_send_reaction_to_user_blocks_untrusted(monkeypatch):
    called = False

    async def fake_reaction(*args, **kwargs):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(main, "_send_reaction", fake_reaction)
    monkeypatch.setattr(main.config, "trusted_recipients", frozenset({ALICE}))

    result = asyncio.run(
        send_reaction_to_user("\U0001f44d", MALLORY, MALLORY, 1782554453770)
    )

    assert "error" in result
    assert called is False
