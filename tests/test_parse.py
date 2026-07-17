import asyncio
import os

from signal_mcp import rpc
from signal_mcp.parse import MessageResponse, Reaction, _envelope_to_response
from signal_mcp.tools import _resolve_group, _send_message, _send_reaction

# Self number used to build "Note to Self" / sync fixtures. Sourced from an env
# var so no real number is ever committed; defaults to a reserved test number.
SELF = os.environ.get("SIGNAL_TEST_NUMBER", "+15555550100")
SELF_UUID = "00000000-0000-0000-0000-000000000000"

# A fake "other party" used for direct-message fixtures.
OTHER = "+11234567890"
ACCOUNT = "+15551234567"

THUMBS_UP = "\U0001f44d"
THUMBS_DOWN = "\U0001f44e"


def _envelope(account: str, **envelope) -> dict:
    """The ``params`` of a signal-cli daemon ``receive`` notification — a single
    ``{"envelope": {...}, "account": ...}`` object."""
    return {"envelope": envelope, "account": account}


def _parse(payload: dict) -> MessageResponse | None:
    return _envelope_to_response(payload)


class FakeClient:
    """Stand-in for SignalRpcClient that records JSON-RPC calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.list_groups_result: list[dict] = []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        if method == "listGroups":
            return self.list_groups_result
        return {"timestamp": 1}


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

# A group text message — groupInfo carries the internal group id.
GROUP_MESSAGE = _envelope(
    ACCOUNT,
    source=OTHER,
    sourceNumber=OTHER,
    sourceName="Bob Sagat",
    sourceDevice=2,
    timestamp=1744185580000,
    dataMessage={
        "timestamp": 1744185580000,
        "message": "hi team",
        "groupInfo": {"groupId": "GID==", "type": "DELIVER"},
        "reaction": None,
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
        sender_name="Bob Sagat",
        group_id=None,
        timestamp=1744185565466,
    )


def test_parse_group_message_captures_group_id():
    result = _parse(GROUP_MESSAGE)
    assert result == MessageResponse(
        message="hi team",
        sender_id=OTHER,
        sender_name="Bob Sagat",
        group_id="GID==",
        timestamp=1744185580000,
    )


def test_parse_direct_reaction():
    result = _parse(DIRECT_REACTION)
    assert result == MessageResponse(
        sender_id=OTHER,
        sender_name="Bob Sagat",
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
    assert result.sender_name == "Tester"
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
    assert _parse({}) is None


def test_send_message_builds_params(monkeypatch):
    """_send_message issues a `send` call with recipient + message params."""
    fake = FakeClient()
    monkeypatch.setattr(rpc, "client", fake)

    asyncio.run(_send_message("yo", OTHER))
    method, params = fake.calls[-1]
    assert method == "send"
    assert params == {"message": "yo", "recipient": [OTHER]}


def test_send_message_to_group_uses_group_id(monkeypatch):
    """Group sends use the `groupId` param instead of `recipient`."""
    fake = FakeClient()
    monkeypatch.setattr(rpc, "client", fake)

    asyncio.run(_send_message("yo", "GID==", is_group=True))
    method, params = fake.calls[-1]
    assert method == "send"
    assert params == {"message": "yo", "groupId": "GID=="}


def test_resolve_group_by_name_or_id(monkeypatch):
    fake = FakeClient()
    fake.list_groups_result = [{"id": "GID==", "name": "#talk-homelab"}]
    monkeypatch.setattr(rpc, "client", fake)

    by_name = asyncio.run(_resolve_group("#talk-homelab"))
    assert by_name is not None and by_name["id"] == "GID=="
    by_id = asyncio.run(_resolve_group("GID=="))
    assert by_id is not None and by_id["id"] == "GID=="
    assert asyncio.run(_resolve_group("nope")) is None


def test_send_reaction_builds_params(monkeypatch):
    """_send_reaction issues a `sendReaction` call with the right params."""
    fake = FakeClient()
    monkeypatch.setattr(rpc, "client", fake)

    asyncio.run(_send_reaction(THUMBS_UP, SELF, SELF, 1782554453770))
    method, params = fake.calls[-1]
    assert method == "sendReaction"
    assert params["emoji"] == THUMBS_UP
    assert params["recipient"] == [SELF]
    assert params["targetAuthor"] == SELF
    assert params["targetTimestamp"] == 1782554453770
    assert params["remove"] is False


def test_send_reaction_remove_passes_flag(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(rpc, "client", fake)

    asyncio.run(_send_reaction(THUMBS_UP, SELF, SELF, 1782554453770, remove=True))
    _, params = fake.calls[-1]
    assert params["remove"] is True
