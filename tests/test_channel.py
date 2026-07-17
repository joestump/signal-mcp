"""Tests for Claude Channel mode: prefix filtering and notification building."""

import asyncio
from unittest.mock import patch

from mcp.types import JSONRPCNotification

from signal_mcp import rpc
from signal_mcp.channel import (
    _attachment_line,
    _format_size,
    _forward_channel_messages,
    _strip_prefix,
)
from signal_mcp.config import config
from signal_mcp.parse import Attachment, MessageResponse, Reaction


class FakeWriteStream:
    """Minimal stand-in for the MCP write stream — collects sent messages."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, msg) -> None:
        self.sent.append(msg)


class FakeClient:
    """Yields pre-loaded messages from next_message() and records calls."""

    def __init__(self, messages: list) -> None:
        self._messages = list(messages)
        self.calls: list[tuple[str, dict]] = []

    async def connect(self) -> None:
        pass

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        return {"timestamp": 1}

    async def next_message(self, timeout: float):
        if not self._messages:
            await asyncio.sleep(3600)
        return self._messages.pop(0)


def _text_msg(
    text: str,
    sender: str = "+1234",
    sender_name: str | None = None,
    group: str | None = None,
    timestamp: int = 1744185565466,
):
    return MessageResponse(
        message=text,
        sender_id=sender,
        sender_name=sender_name,
        group_id=group,
        timestamp=timestamp,
    )


def _attachment(
    path="/tmp/attachments/abc123.png",
    content_type="image/png",
    filename="photo.png",
    attachment_id="abc123.png",
    size=250880,
):
    return Attachment(
        id=attachment_id,
        content_type=content_type,
        filename=filename,
        size=size,
        path=path,
    )


def _attachment_msg(
    text=None,
    attachments=None,
    sender="+1234",
    timestamp=1744185565466,
):
    return MessageResponse(
        message=text,
        sender_id=sender,
        timestamp=timestamp,
        attachments=attachments if attachments is not None else [_attachment()],
    )


def _run_forwarder(messages, prefix=""):
    """Run the forwarder just long enough to drain queued messages, then cancel.
    Returns (sent_notifications, fake_client).
    """
    config.prefix = prefix
    fake_client = FakeClient(messages)
    stream = FakeWriteStream()

    async def _runner():
        with patch.object(rpc, "client", fake_client):
            task = asyncio.create_task(_forward_channel_messages(stream))
            await asyncio.sleep(0.15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return stream.sent, fake_client

    return asyncio.run(_runner())


def test_forwards_text_message():
    """A plain text message is forwarded as a channel notification."""
    sent, _ = _run_forwarder([_text_msg("hello world", sender="+15551234567")])
    assert len(sent) == 1
    notif = sent[0].root
    assert isinstance(notif, JSONRPCNotification)
    assert notif.method == "notifications/claude/channel"
    params = notif.params
    assert params is not None
    assert params["content"] == "hello world"
    assert params["meta"]["sender"] == "+15551234567"
    assert "group" not in params["meta"]
    assert "sender_name" not in params["meta"]


def test_forwards_sender_name_in_meta():
    """The sender's profile name is included as sender_name when known."""
    sent, _ = _run_forwarder(
        [_text_msg("hello", sender="+15551234567", sender_name="Bob Sagat")]
    )
    assert len(sent) == 1
    assert sent[0].root.params["meta"]["sender_name"] == "Bob Sagat"


def test_forwards_group_message_with_meta():
    """Group messages include the group id in meta."""
    sent, _ = _run_forwarder([_text_msg("hi team", sender="+111", group="group-123==")])
    assert len(sent) == 1
    assert sent[0].root.params["meta"]["group"] == "group-123=="


def test_skips_non_text_messages():
    """Reactions (message=None) are not forwarded."""
    reaction = MessageResponse(
        sender_id="+1234",
        reaction=Reaction(
            emoji="\U0001f44d", target_author="+1234", target_timestamp=1
        ),
    )
    sent, _ = _run_forwarder([reaction])
    assert len(sent) == 0


def test_prefix_filters_and_strips():
    """Only messages matching the prefix are forwarded, with prefix stripped."""
    sent, _ = _run_forwarder(
        [
            _text_msg("cc run tests"),
            _text_msg("buy milk"),
            _text_msg("CC deploy now"),
        ],
        prefix="cc",
    )
    assert len(sent) == 2
    assert sent[0].root.params["content"] == "run tests"
    assert sent[1].root.params["content"] == "deploy now"


def test_prefix_requires_word_boundary():
    """Prefix `cc` must not match `ccdeploy` — only whole-word matches count."""
    sent, _ = _run_forwarder(
        [
            _text_msg("ccdeploy now"),
            _text_msg("cc deploy now"),
        ],
        prefix="cc",
    )
    assert len(sent) == 1
    assert sent[0].root.params["content"] == "deploy now"


def test_strip_prefix_word_boundary_cases():
    assert _strip_prefix("cc run tests", "cc") == "run tests"
    assert _strip_prefix("  CC run tests", "cc") == "run tests"
    assert _strip_prefix("ccdeploy now", "cc") is None
    assert _strip_prefix("cc_deploy now", "cc") is None
    assert _strip_prefix("buy milk", "cc") is None
    assert _strip_prefix("cc", "cc") == ""
    # A prefix that itself ends in punctuation carries its own boundary.
    assert _strip_prefix("cc:deploy", "cc:") == "deploy"


def test_notification_is_valid_jsonrpc():
    """The notification serializes to valid JSON-RPC 2.0."""
    sent, _ = _run_forwarder([_text_msg("test")])
    raw = sent[0].model_dump(by_alias=True, exclude_none=True)
    assert raw["jsonrpc"] == "2.0"
    assert raw["method"] == "notifications/claude/channel"
    assert "content" in raw["params"]
    assert "meta" in raw["params"]


def test_auto_marks_direct_message_as_read():
    """Forwarded direct messages trigger a sendReceipt call."""
    ts = 1744185565466
    _, fake = _run_forwarder(
        [
            _text_msg("hello", sender="+15551234567", timestamp=ts),
        ]
    )
    receipt_calls = [c for c in fake.calls if c[0] == "sendReceipt"]
    assert len(receipt_calls) == 1
    method, params = receipt_calls[0]
    assert params["recipient"] == ["+15551234567"]
    assert params["targetTimestamp"] == ts
    assert params["type"] == "read"


def test_auto_marks_group_message_as_read():
    """Forwarded group messages trigger a sendReceipt addressed to the author."""
    ts = 1744185565466
    _, fake = _run_forwarder(
        [
            _text_msg("hello", sender="+111", group="group-123==", timestamp=ts),
        ]
    )
    receipt_calls = [c for c in fake.calls if c[0] == "sendReceipt"]
    assert len(receipt_calls) == 1
    method, params = receipt_calls[0]
    assert params["recipient"] == ["+111"]
    assert "groupId" not in params
    assert params["targetTimestamp"] == ts


def test_auto_mark_read_skipped_when_no_timestamp():
    """Messages without a timestamp don't trigger a read receipt."""
    msg = MessageResponse(message="hello", sender_id="+15551234567", timestamp=None)
    _, fake = _run_forwarder([msg])
    receipt_calls = [c for c in fake.calls if c[0] == "sendReceipt"]
    assert len(receipt_calls) == 0


def test_auto_mark_read_skipped_for_filtered_messages():
    """Messages filtered out by prefix don't trigger a read receipt."""
    ts = 1744185565466
    _, fake = _run_forwarder(
        [_text_msg("buy milk", sender="+111", timestamp=ts)],
        prefix="cc",
    )
    receipt_calls = [c for c in fake.calls if c[0] == "sendReceipt"]
    assert len(receipt_calls) == 0


# --- attachments -------------------------------------------------------------


def test_format_size():
    assert _format_size(None) == "unknown size"
    assert _format_size(0) == "0 B"
    assert _format_size(512) == "512 B"
    assert _format_size(1536) == "1.5 KB"
    assert _format_size(250880) == "245 KB"
    assert _format_size(5 * 1024 * 1024) == "5 MB"


def test_attachment_line_variants():
    """Path form when resolved; filename/id fallback + note when missing."""
    resolved = _attachment()
    assert _attachment_line(resolved) == (
        "[attachment: /tmp/attachments/abc123.png (image/png, 245 KB)]"
    )
    missing = _attachment(path=None)
    assert _attachment_line(missing) == (
        "[attachment: photo.png (image/png, 245 KB) — file not available locally]"
    )
    missing_no_name = _attachment(path=None, filename=None)
    assert _attachment_line(missing_no_name) == (
        "[attachment: abc123.png (image/png, 245 KB) — file not available locally]"
    )


def test_forwards_image_with_caption():
    """Image + caption: content is the caption plus one annotation line."""
    sent, _ = _run_forwarder([_attachment_msg(text="look at this")])
    assert len(sent) == 1
    assert sent[0].root.params["content"] == (
        "look at this\n[attachment: /tmp/attachments/abc123.png (image/png, 245 KB)]"
    )


def test_forwards_attachment_only_message():
    """A message with attachments but no text forwards as annotation only."""
    sent, _ = _run_forwarder([_attachment_msg()])
    assert len(sent) == 1
    assert sent[0].root.params["content"] == (
        "[attachment: /tmp/attachments/abc123.png (image/png, 245 KB)]"
    )


def test_forwards_multiple_attachments_one_line_each():
    """Every attachment contributes its own annotation line."""
    sent, _ = _run_forwarder(
        [
            _attachment_msg(
                text="two files",
                attachments=[
                    _attachment(),
                    _attachment(
                        path="/tmp/attachments/doc.pdf",
                        content_type="application/pdf",
                        filename="doc.pdf",
                        attachment_id="doc.pdf",
                        size=1536,
                    ),
                ],
            )
        ]
    )
    assert len(sent) == 1
    assert sent[0].root.params["content"] == (
        "two files\n"
        "[attachment: /tmp/attachments/abc123.png (image/png, 245 KB)]\n"
        "[attachment: /tmp/attachments/doc.pdf (application/pdf, 1.5 KB)]"
    )


def test_forwards_attachment_without_local_file():
    """path=None uses the no-path annotation form (metadata only)."""
    sent, _ = _run_forwarder([_attachment_msg(attachments=[_attachment(path=None)])])
    assert len(sent) == 1
    assert sent[0].root.params["content"] == (
        "[attachment: photo.png (image/png, 245 KB) — file not available locally]"
    )


def test_prefix_drops_attachment_only_message():
    """With a prefix configured, attachment-only messages fail closed."""
    sent, fake = _run_forwarder([_attachment_msg()], prefix="cc")
    assert sent == []
    assert [c for c in fake.calls if c[0] == "sendReceipt"] == []


def test_prefix_applies_to_caption_of_attachment_message():
    """The prefix gates on the caption text; annotation lines are appended."""
    sent, _ = _run_forwarder(
        [
            _attachment_msg(text="cc check this out"),
            _attachment_msg(text="not for claude"),
        ],
        prefix="cc",
    )
    assert len(sent) == 1
    assert sent[0].root.params["content"] == (
        "check this out\n[attachment: /tmp/attachments/abc123.png (image/png, 245 KB)]"
    )


def test_reactions_still_skipped_with_attachment_support():
    """Reactions are never forwarded, even now that empty text can forward."""
    reaction = MessageResponse(
        sender_id="+1234",
        reaction=Reaction(
            emoji="\U0001f44d", target_author="+1234", target_timestamp=1
        ),
    )
    truly_empty = MessageResponse(sender_id="+1234", timestamp=1)
    sent, fake = _run_forwarder([reaction, truly_empty])
    assert sent == []
    assert [c for c in fake.calls if c[0] == "sendReceipt"] == []


def test_receipt_sent_for_forwarded_attachment_message():
    """Forwarded attachment-only messages still trigger a read receipt."""
    ts = 1744185565466
    sent, fake = _run_forwarder([_attachment_msg(sender="+15551234567", timestamp=ts)])
    assert len(sent) == 1
    receipt_calls = [c for c in fake.calls if c[0] == "sendReceipt"]
    assert len(receipt_calls) == 1
    _, params = receipt_calls[0]
    assert params["recipient"] == ["+15551234567"]
    assert params["targetTimestamp"] == ts
    assert params["type"] == "read"


def test_untrusted_sender_dropped_before_attachment_handling(monkeypatch):
    """The trusted-sender gate runs first: untrusted attachment messages are
    dropped with no notification and no read receipt."""
    monkeypatch.setattr(config, "trusted_senders", frozenset({"+15550001111"}))
    sent, fake = _run_forwarder(
        [
            _attachment_msg(sender="+19995550000"),
            _attachment_msg(text="cc hi", sender="+19995550000"),
        ]
    )
    assert sent == []
    assert [c for c in fake.calls if c[0] == "sendReceipt"] == []
