"""Signal @-mentions: ``bodyRanges`` parsed on receive, emitted on the send path.

Fixtures use reserved synthetic UUIDs only: no identity, phone number, or
token from any real conversation appears here.
"""

import asyncio

from signal_mcp import rpc
from signal_mcp.channel import _base_meta
from signal_mcp.parse import (
    MENTION_PLACEHOLDER,
    Mention,
    MessageResponse,
    _envelope_to_response,
    mentions_to_body_ranges,
    render_mentions,
)
from signal_mcp.tools import _send_message

ACCOUNT = "+15551234567"
OTHER = "+11234567890"

BOB_UUID = "11111111-1111-1111-1111-111111111111"
CAROL_UUID = "22222222-2222-2222-2222-222222222222"

GROUP_ID = "GROUP_ID=="


class FakeClient:
    """Stand-in for SignalRpcClient that records JSON-RPC calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        return {"timestamp": 1}


def _group_message(body: str, **data_message) -> dict:
    """A group text message envelope, with any extra dataMessage fields."""
    return {
        "envelope": {
            "source": OTHER,
            "sourceNumber": OTHER,
            "sourceName": "Bob Example",
            "timestamp": 1744185565466,
            "dataMessage": {
                "timestamp": 1744185565466,
                "groupInfo": {"groupId": GROUP_ID},
                "message": body,
                **data_message,
            },
        },
        "account": ACCOUNT,
    }


def _ranges(*entries: tuple[int, int, str]) -> list[dict]:
    return [
        {"start": start, "length": length, "mentionUuid": uuid}
        for start, length, uuid in entries
    ]


# Receive path.


def test_group_mention_is_parsed_and_rendered():
    """A mention arrives as a placeholder plus a bodyRanges entry."""
    payload = _group_message(
        f"hey {MENTION_PLACEHOLDER} can you take this?",
        bodyRanges=_ranges((4, 1, BOB_UUID)),
    )

    result = _envelope_to_response(payload)

    assert result is not None
    assert result.message == f"hey @[{BOB_UUID}] can you take this?"
    (mention,) = result.mentions
    assert mention == Mention(uuid=BOB_UUID, start=4, length=1)


def test_mention_offsets_survive_emoji_before_the_span():
    """Offsets count UTF-16 code units, so an emoji shifts the str index."""
    payload = _group_message(
        f"\U0001f680 {MENTION_PLACEHOLDER} hi",
        bodyRanges=_ranges((3, 1, BOB_UUID)),
    )

    result = _envelope_to_response(payload)

    assert result is not None
    assert result.message == f"\U0001f680 @[{BOB_UUID}] hi"


def test_multiple_mentions_keep_body_order():
    payload = _group_message(
        f"{MENTION_PLACEHOLDER} and {MENTION_PLACEHOLDER} check this",
        bodyRanges=_ranges((0, 1, CAROL_UUID), (6, 1, BOB_UUID)),
    )

    result = _envelope_to_response(payload)

    assert result is not None
    assert result.message == f"@[{CAROL_UUID}] and @[{BOB_UUID}] check this"
    assert [mention.uuid for mention in result.mentions] == [CAROL_UUID, BOB_UUID]


def test_non_mention_body_ranges_are_ignored():
    """Style and short-code ranges carry no mentionUuid, so they are dropped."""
    payload = _group_message(
        "plain text",
        bodyRanges=[{"start": 0, "length": 5, "style": "BOLD"}],
    )

    result = _envelope_to_response(payload)

    assert result is not None
    assert result.message == "plain text"
    assert result.mentions == []


def test_malformed_body_range_does_not_break_the_message():
    payload = _group_message(
        f"ok {MENTION_PLACEHOLDER}",
        bodyRanges=[
            "nonsense",
            {"start": "not-a-number", "length": 1, "mentionUuid": BOB_UUID},
        ],
    )

    result = _envelope_to_response(payload)

    assert result is not None
    # The body is delivered as received: no range resolved into it.
    assert result.message == f"ok {MENTION_PLACEHOLDER}"
    (mention,) = result.mentions
    assert mention == Mention(uuid=BOB_UUID, start=None, length=1)


def test_literal_name_span_is_left_as_received():
    """A client that keeps the literal @name text needs no substitution."""
    payload = _group_message(
        "ok @bob",
        bodyRanges=_ranges((3, 4, BOB_UUID)),
    )

    result = _envelope_to_response(payload)

    assert result is not None
    assert result.message == "ok @bob"
    assert [mention.uuid for mention in result.mentions] == [BOB_UUID]


def test_range_pointing_outside_the_body_is_ignored():
    payload = _group_message("short", bodyRanges=_ranges((99, 1, BOB_UUID)))

    result = _envelope_to_response(payload)

    assert result is not None
    assert result.message == "short"
    assert [mention.uuid for mention in result.mentions] == [BOB_UUID]


def test_sync_sent_mention_is_parsed():
    """A message sent from another linked device keeps its mention metadata."""
    payload = {
        "envelope": {
            "source": ACCOUNT,
            "sourceNumber": ACCOUNT,
            "timestamp": 1744185565466,
            "syncMessage": {
                "sentMessage": {
                    "timestamp": 1744185565466,
                    "groupInfo": {"groupId": GROUP_ID},
                    "message": f"noted {MENTION_PLACEHOLDER}",
                    "bodyRanges": _ranges((6, 1, BOB_UUID)),
                }
            },
        },
        "account": ACCOUNT,
    }

    result = _envelope_to_response(payload)

    assert result is not None
    assert result.is_sync_sent
    assert result.message == f"noted @[{BOB_UUID}]"


def test_message_without_body_ranges_is_unchanged():
    result = _envelope_to_response(_group_message("no mentions here"))

    assert result is not None
    assert result.message == "no mentions here"
    assert result.mentions == []


def test_render_mentions_prefers_a_known_display_name():
    body = f"hi {MENTION_PLACEHOLDER}"
    named = Mention(uuid=BOB_UUID, start=3, length=1, name="Bob")

    assert render_mentions(body, [named]) == "hi @[Bob]"


# Channel delivery.


def test_base_meta_lists_mentioned_accounts():
    msg = MessageResponse(
        message=f"hey @[{BOB_UUID}]",
        sender_id=OTHER,
        group_id=GROUP_ID,
        timestamp=1744185565466,
        mentions=[
            Mention(uuid=BOB_UUID, start=4, length=1),
            Mention(uuid=CAROL_UUID, start=9),
        ],
    )

    meta = _base_meta(msg)

    assert meta["mentions"] == f"{BOB_UUID}, {CAROL_UUID}"
    assert meta["group"] == GROUP_ID


def test_base_meta_omits_mentions_when_there_are_none():
    meta = _base_meta(MessageResponse(message="plain", sender_id=OTHER))

    assert "mentions" not in meta


# Send path.


def test_send_message_forwards_body_ranges(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(rpc, "client", fake)

    asyncio.run(_send_message(f"ok @[{BOB_UUID}] thanks", OTHER))

    method, params = fake.calls[-1]
    assert method == "send"
    assert params == {
        "message": f"ok {MENTION_PLACEHOLDER} thanks",
        "recipient": [OTHER],
        "bodyRanges": _ranges((3, 1, BOB_UUID)),
    }


def test_send_message_counts_offsets_in_utf16_units(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(rpc, "client", fake)

    asyncio.run(_send_message(f"\U0001f680 @[{CAROL_UUID}]", OTHER))

    _, params = fake.calls[-1]
    assert params["message"] == f"\U0001f680 {MENTION_PLACEHOLDER}"
    assert params["bodyRanges"] == _ranges((3, 1, CAROL_UUID))


def test_send_message_without_mentions_sends_no_body_ranges(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(rpc, "client", fake)

    # A bare UUID in square brackets is not a mention span: it stays as text.
    asyncio.run(_send_message(f"nothing to mention [{BOB_UUID}]", OTHER))

    _, params = fake.calls[-1]
    assert "bodyRanges" not in params
    assert params["message"] == f"nothing to mention [{BOB_UUID}]"


def test_send_message_to_group_uses_group_id_and_ranges(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(rpc, "client", fake)

    asyncio.run(_send_message(f"cc @[{CAROL_UUID}]", "GID==", is_group=True))

    _, params = fake.calls[-1]
    assert params == {
        "message": f"cc {MENTION_PLACEHOLDER}",
        "groupId": "GID==",
        "bodyRanges": _ranges((3, 1, CAROL_UUID)),
    }


def test_send_message_normalizes_an_uppercase_uuid(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(rpc, "client", fake)

    asyncio.run(_send_message(f"hi @[{BOB_UUID.upper()}]", OTHER))

    _, params = fake.calls[-1]
    assert params["bodyRanges"] == _ranges((3, 1, BOB_UUID))


def test_mentions_to_body_ranges_handles_several_spans():
    body, ranges = mentions_to_body_ranges(f"hi @[{BOB_UUID}] and @[{CAROL_UUID}] go")

    assert body == f"hi {MENTION_PLACEHOLDER} and {MENTION_PLACEHOLDER} go"
    assert ranges == _ranges((3, 1, BOB_UUID), (9, 1, CAROL_UUID))


def test_receive_then_send_round_trips_the_mention():
    """A body received with a mention can be sent back and mentions the same account."""
    received = _envelope_to_response(
        _group_message(
            f"ping {MENTION_PLACEHOLDER} please",
            bodyRanges=_ranges((5, 1, BOB_UUID)),
        )
    )

    assert received is not None
    body, ranges = mentions_to_body_ranges(received.message or "")

    assert body == f"ping {MENTION_PLACEHOLDER} please"
    assert ranges == _ranges((5, 1, BOB_UUID))
