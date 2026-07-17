import asyncio
import os

from signal_mcp import rpc
from signal_mcp.config import config
from signal_mcp.parse import (
    Attachment,
    MessageResponse,
    Reaction,
    _envelope_to_response,
)
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

# Attachment metadata as signal-cli 0.14.6 reports it: `id` is the name the
# file is stored under on disk (WITH extension), `filename` is the sender's
# original file name.
IMAGE_ID = "0oHirH8e8bm9oPM0NJ3B.png"
IMAGE_ATTACHMENT = {
    "contentType": "image/png",
    "filename": "photo.png",
    "id": IMAGE_ID,
    "size": 12345,
}


def _image_with_caption() -> dict:
    """A direct message carrying both a text caption and an image."""
    return _envelope(
        ACCOUNT,
        source=OTHER,
        sourceNumber=OTHER,
        sourceName="Bob Sagat",
        sourceDevice=2,
        timestamp=1744185590000,
        dataMessage={
            "timestamp": 1744185590000,
            "message": "look at this",
            "groupInfo": None,
            "reaction": None,
            "attachments": [dict(IMAGE_ATTACHMENT)],
        },
    )


def _attachment_only(attachment: dict | None = None) -> dict:
    """A bare image with no text body — must still produce a response."""
    return _envelope(
        ACCOUNT,
        source=OTHER,
        sourceNumber=OTHER,
        sourceName="Bob Sagat",
        sourceDevice=2,
        timestamp=1744185600000,
        dataMessage={
            "timestamp": 1744185600000,
            "message": None,
            "groupInfo": None,
            "reaction": None,
            "attachments": [dict(attachment or IMAGE_ATTACHMENT)],
        },
    )


def _synced_attachment() -> dict:
    """An attachment sent from another linked device — arrives as
    syncMessage.sentMessage.attachments, not a dataMessage."""
    return _envelope(
        SELF,
        source=SELF,
        sourceNumber=SELF,
        sourceUuid=SELF_UUID,
        sourceName="Tester",
        sourceDevice=1,
        timestamp=1782555400000,
        syncMessage={
            "sentMessage": {
                "destination": SELF,
                "timestamp": 1782555400000,
                "message": None,
                "reaction": None,
                "attachments": [dict(IMAGE_ATTACHMENT)],
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


def test_parse_image_with_caption(tmp_path):
    """An image with a caption keeps the text body and resolves the file path."""
    stored = tmp_path / IMAGE_ID
    stored.write_bytes(b"\x89PNG fake")

    result = _envelope_to_response(_image_with_caption(), str(tmp_path))
    assert result == MessageResponse(
        message="look at this",
        sender_id=OTHER,
        sender_name="Bob Sagat",
        group_id=None,
        timestamp=1744185590000,
        attachments=[
            Attachment(
                id=IMAGE_ID,
                content_type="image/png",
                filename="photo.png",
                size=12345,
                path=str(stored.resolve()),
            )
        ],
    )


def test_parse_attachment_only_message(tmp_path):
    """An envelope with attachments but no text body still yields a response."""
    stored = tmp_path / IMAGE_ID
    stored.write_bytes(b"\x89PNG fake")

    result = _envelope_to_response(_attachment_only(), str(tmp_path))
    assert result is not None
    assert result.message is None
    assert result.sender_id == OTHER
    assert result.timestamp == 1744185600000
    assert result.attachments == [
        Attachment(
            id=IMAGE_ID,
            content_type="image/png",
            filename="photo.png",
            size=12345,
            path=str(stored.resolve()),
        )
    ]


def test_parse_synced_attachment(tmp_path):
    """Attachments sent from another linked device arrive on
    syncMessage.sentMessage and must be parsed the same way."""
    stored = tmp_path / IMAGE_ID
    stored.write_bytes(b"\x89PNG fake")

    result = _envelope_to_response(_synced_attachment(), str(tmp_path))
    assert result is not None
    assert result.message is None
    assert result.sender_name == "Tester"
    assert result.attachments == [
        Attachment(
            id=IMAGE_ID,
            content_type="image/png",
            filename="photo.png",
            size=12345,
            path=str(stored.resolve()),
        )
    ]


def test_parse_attachment_missing_file_keeps_metadata(tmp_path):
    """When the stored file is absent, path is None but metadata survives."""
    result = _envelope_to_response(_image_with_caption(), str(tmp_path))
    assert result is not None
    assert result.message == "look at this"
    assert result.attachments == [
        Attachment(
            id=IMAGE_ID,
            content_type="image/png",
            filename="photo.png",
            size=12345,
            path=None,
        )
    ]


def test_parse_attachments_defaults_to_configured_dir(tmp_path, monkeypatch):
    """Without an explicit dir, files are resolved via config.attachments_dir."""
    stored = tmp_path / IMAGE_ID
    stored.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(config, "attachments_dir", str(tmp_path))

    result = _parse(_attachment_only())
    assert result is not None
    assert result.attachments[0].path == str(stored.resolve())


def test_messages_without_attachments_have_empty_list():
    result = _parse(DIRECT_MESSAGE)
    assert result is not None
    assert result.attachments == []


def test_hostile_attachment_ids_do_not_escape_dir(tmp_path):
    """Traversal and absolute ids must never resolve outside attachments_dir.

    The target files genuinely exist, so a bare existence check would have
    resolved them — containment is what must reject them, yielding path=None
    while keeping the metadata (same behavior as a missing file).
    """
    attachments_dir = tmp_path / "attachments"
    attachments_dir.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("s3kr1t")

    for hostile_id in ("../secret.txt", "../../etc/hosts", "/etc/hosts"):
        envelope = _attachment_only({**IMAGE_ATTACHMENT, "id": hostile_id})
        result = _envelope_to_response(envelope, str(attachments_dir))
        assert result is not None
        (attachment,) = result.attachments
        assert attachment.path is None, hostile_id
        assert attachment.id == hostile_id
        assert attachment.content_type == "image/png"
        assert attachment.filename == "photo.png"
        assert attachment.size == 12345


def test_symlink_escaping_attachments_dir_is_not_resolved(tmp_path):
    """A symlink inside the dir pointing outside it must not leak the target."""
    attachments_dir = tmp_path / "attachments"
    attachments_dir.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("s3kr1t")
    (attachments_dir / "link.png").symlink_to(secret)

    envelope = _attachment_only({**IMAGE_ATTACHMENT, "id": "link.png"})
    result = _envelope_to_response(envelope, str(attachments_dir))
    assert result is not None
    (attachment,) = result.attachments
    assert attachment.path is None
    assert attachment.id == "link.png"
    assert attachment.size == 12345


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
