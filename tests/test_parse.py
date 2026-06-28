import asyncio
import json
import os

from signal_mcp import main
from signal_mcp.main import (
    MessageResponse,
    Reaction,
    _parse_receive_output,
    _send_reaction,
)

# Self number used to build "Note to Self" / sync fixtures. Sourced from an env
# var so no real number is ever committed; defaults to a reserved test number.
SELF = os.environ.get("SIGNAL_TEST_NUMBER", "+15555550100")
SELF_UUID = "00000000-0000-0000-0000-000000000000"

# A fake "other party" used for direct-message fixtures.
OTHER = "+11234567890"
ACCOUNT = "+15551234567"

THUMBS_UP = "\U0001f44d"
THUMBS_DOWN = "\U0001f44e"


def _envelope(account: str, **envelope) -> str:
    """signal-cli --output=json emits one JSON object per line."""
    return json.dumps({"envelope": envelope, "account": account})


def _parse(output: str) -> MessageResponse | None:
    return asyncio.run(_parse_receive_output(output))


# A direct text message from another user.
DIRECT_MESSAGE = _envelope(
    ACCOUNT,
    source=OTHER,
    sourceNumber=OTHER,
    sourceName="Bob Sagat",
    sourceDevice=2,
    timestamp=1744185565466,
    dataMessage={
        "timestamp": 1744185565466,
        "message": "yo",
        "groupInfo": None,
        "reaction": None,
    },
)

# A reaction from another user to one of our messages.
DIRECT_REACTION = _envelope(
    ACCOUNT,
    source=OTHER,
    sourceName="Bob Sagat",
    sourceDevice=2,
    timestamp=1744185570000,
    dataMessage={
        "timestamp": 1744185570000,
        "message": None,
        "reaction": {
            "emoji": THUMBS_UP,
            "targetAuthor": ACCOUNT,
            "targetSentTimestamp": 1744185565466,
            "isRemove": False,
        },
    },
)

# Synced reaction shape captured from signal-cli 0.14.5 (number scrubbed) —
# reacting to a "Note to Self" message arrives as syncMessage.sentMessage.reaction,
# NOT a dataMessage. The plain-text format drops these entirely.
SYNC_REACTION = _envelope(
    SELF,
    source=SELF,
    sourceNumber=SELF,
    sourceUuid=SELF_UUID,
    sourceName="Tester",
    sourceDevice=1,
    timestamp=1782555227946,
    syncMessage={
        "sentMessage": {
            "destination": SELF,
            "timestamp": 1782555227946,
            "message": None,
            "reaction": {
                "emoji": THUMBS_UP,
                "targetAuthor": SELF,
                "targetSentTimestamp": 1782554453770,
                "isRemove": False,
            },
        }
    },
)

# A synced reaction removal (un-react).
SYNC_REACTION_REMOVE = _envelope(
    SELF,
    source=SELF,
    sourceDevice=1,
    timestamp=1782555300000,
    syncMessage={
        "sentMessage": {
            "timestamp": 1782555300000,
            "message": None,
            "reaction": {
                "emoji": THUMBS_DOWN,
                "targetAuthor": SELF,
                "targetSentTimestamp": 1782554075267,
                "isRemove": True,
            },
        }
    },
)

# A delivery receipt — should be ignored entirely.
RECEIPT = _envelope(
    ACCOUNT,
    source=OTHER,
    sourceDevice=2,
    timestamp=1744185565739,
    receiptMessage={
        "when": 1744185565739,
        "isDelivery": True,
        "isRead": False,
        "timestamps": [1744185565466],
    },
)


def test_parse_direct_message():
    result = _parse(DIRECT_MESSAGE)
    assert result == MessageResponse(
        message="yo",
        sender_id=OTHER,
        group_name=None,
        timestamp=1744185565466,
    )


def test_parse_direct_reaction():
    result = _parse(DIRECT_REACTION)
    assert result == MessageResponse(
        sender_id=OTHER,
        timestamp=1744185570000,
        reaction=Reaction(
            emoji="\U0001f44d",
            target_author=ACCOUNT,
            target_timestamp=1744185565466,
            is_remove=False,
        ),
    )


def test_parse_sync_reaction():
    """Reacting to a Note-to-Self message — the case text parsing can't see."""
    result = _parse(SYNC_REACTION)
    assert result is not None
    assert result.message is None
    assert result.reaction == Reaction(
        emoji="\U0001f44d",
        target_author=SELF,
        target_timestamp=1782554453770,
        is_remove=False,
    )


def test_parse_sync_reaction_remove():
    result = _parse(SYNC_REACTION_REMOVE)
    assert result is not None
    assert result.reaction is not None
    assert result.reaction.emoji == "\U0001f44e"
    assert result.reaction.is_remove is True


def test_receipts_are_ignored():
    assert _parse(RECEIPT) is None
    assert _parse("") is None


def test_first_meaningful_envelope_wins():
    """Receipts before a message should be skipped, message returned."""
    combined = "\n".join([RECEIPT, DIRECT_MESSAGE])
    result = _parse(combined)
    assert result is not None
    assert result.message == "yo"


def test_send_reaction_builds_command(monkeypatch):
    """_send_reaction shells out to `signal-cli sendReaction` with the right flags."""
    captured = {}

    async def fake_run(cmd: str):
        captured["cmd"] = cmd
        return ("", "", 0)

    monkeypatch.setattr(main, "_run_signal_cli", fake_run)
    monkeypatch.setattr(main.config, "user_id", SELF)

    ok = asyncio.run(
        _send_reaction(
            "\U0001f44d",
            SELF,
            SELF,
            1782554453770,
        )
    )
    assert ok is True
    cmd = captured["cmd"]
    assert "sendReaction" in cmd
    assert "-e " in cmd
    assert f"-a {SELF}" in cmd
    assert "-t 1782554453770" in cmd
    assert "-r" not in cmd  # not a removal


def test_send_reaction_remove_passes_flag(monkeypatch):
    captured = {}

    async def fake_run(cmd: str):
        captured["cmd"] = cmd
        return ("", "", 0)

    monkeypatch.setattr(main, "_run_signal_cli", fake_run)
    monkeypatch.setattr(main.config, "user_id", SELF)

    asyncio.run(
        _send_reaction(
            "\U0001f44d",
            SELF,
            SELF,
            1782554453770,
            remove=True,
        )
    )
    assert "-r" in captured["cmd"]
