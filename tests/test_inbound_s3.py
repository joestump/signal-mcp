"""Tests for inbound attachment upload to S3 + presigned URLs (#21).

Covers the s3.store_inbound_attachments orchestration and its wiring into the
channel forwarder:

- upload + presign flow sets Attachment.url and the channel line carries the URL,
- an upload failure falls back to the local path WITHOUT dropping the message,
- S3-disabled leaves behavior unchanged (url stays None),
- attachments with no local file are skipped, and
- the deterministic {YYYY}/{MM}/{ts}-{id} key layout.

S3 API calls are stubbed with botocore.stub.Stubber (no network); the forwarder
wiring tests fake upload_file/presign so no boto3 is needed.
"""

import asyncio
import re
from dataclasses import fields, replace
from unittest.mock import patch

import boto3
import pytest
from botocore.stub import ANY, Stubber

from signal_mcp import rpc, s3
from signal_mcp.channel import _attachment_line, _forward_channel_messages
from signal_mcp.config import config
from signal_mcp.parse import Attachment, MessageResponse

TS = 1744185565466  # a fixed message timestamp (ms since epoch)


@pytest.fixture(autouse=True)
def _restore_config():
    """store_inbound_attachments reads the global config; restore it each test."""
    snapshot = replace(config)
    yield
    for f in fields(config):
        setattr(config, f.name, getattr(snapshot, f.name))


@pytest.fixture(autouse=True)
def _reset_client():
    s3.reset_client()
    yield
    s3.reset_client()


def _stubbed_client():
    """A real boto3 client with fake static creds (never hits the network)."""
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=BotoConfig(signature_version="s3v4"),
    )


def _attachment(**overrides) -> Attachment:
    base = Attachment(
        id="abc123.png",
        content_type="image/png",
        filename="photo.png",
        size=250880,
        path="/tmp/attachments/abc123.png",
    )
    return replace(base, **overrides)


# ---------------------------------------------------------------------------
# Object key layout
# ---------------------------------------------------------------------------


def test_inbound_key_layout():
    key = s3._inbound_key(TS, "abc123.png")
    assert re.fullmatch(rf"\d{{4}}/\d{{2}}/{TS}-abc123\.png", key)


def test_inbound_key_missing_timestamp_uses_epoch():
    assert s3._inbound_key(None, "x.bin") == "1970/01/0-x.bin"


# ---------------------------------------------------------------------------
# store_inbound_attachments: upload + presign (Stubber, no network)
# ---------------------------------------------------------------------------


def test_store_uploads_and_sets_presigned_url(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    monkeypatch.setattr(config, "s3_prefix", "signal-mcp/")
    client = _stubbed_client()
    monkeypatch.setattr(s3, "_client", client)

    src = tmp_path / "abc123.png"
    src.write_bytes(b"fake-image-bytes")
    att = _attachment(path=str(src))
    msg = MessageResponse(message="hi", sender_id="+1", timestamp=TS, attachments=[att])

    expected_key = s3.object_key(s3._inbound_key(TS, "abc123.png"))
    stubber = Stubber(client)
    stubber.add_response(
        "put_object",
        {"ETag": '"abc"'},
        {
            "Bucket": "attachments",
            "Key": expected_key,
            "Body": ANY,
            "ContentType": "image/png",
        },
    )
    with stubber:
        asyncio.run(s3.store_inbound_attachments(msg))
        stubber.assert_no_pending_responses()

    assert att.url is not None
    assert expected_key in att.url
    assert "X-Amz-Expires" in att.url  # it is a presigned URL


def test_store_uses_octet_stream_when_content_type_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    monkeypatch.setattr(config, "s3_prefix", "")
    client = _stubbed_client()
    monkeypatch.setattr(s3, "_client", client)

    src = tmp_path / "blob"
    src.write_bytes(b"x")
    att = _attachment(id="blob", content_type=None, filename=None, path=str(src))
    msg = MessageResponse(sender_id="+1", timestamp=TS, attachments=[att])

    stubber = Stubber(client)
    stubber.add_response(
        "put_object",
        {"ETag": '"e"'},
        {
            "Bucket": "attachments",
            "Key": s3._inbound_key(TS, "blob"),
            "Body": ANY,
            "ContentType": "application/octet-stream",
        },
    )
    with stubber:
        asyncio.run(s3.store_inbound_attachments(msg))
        stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Failure isolation and skips
# ---------------------------------------------------------------------------


def test_store_failure_falls_back_to_local_path(monkeypatch, tmp_path):
    """An upload failure leaves url=None and never raises (delivery proceeds)."""
    monkeypatch.setattr(config, "s3_bucket", "attachments")

    async def boom(*args, **kwargs):
        raise s3.S3Error("upload exploded")

    monkeypatch.setattr(s3, "upload_file", boom)

    src = tmp_path / "abc123.png"
    src.write_bytes(b"x")
    att = _attachment(path=str(src))
    msg = MessageResponse(sender_id="+1", timestamp=TS, attachments=[att])

    asyncio.run(s3.store_inbound_attachments(msg))  # must not raise

    assert att.url is None
    assert _attachment_line(att) == f"[attachment: {src} (image/png, 245 KB)]"


def test_store_times_out_and_falls_back(monkeypatch):
    """A hung upload is bounded by _STORE_TIMEOUT — it never stalls the caller."""
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    monkeypatch.setattr(s3, "_STORE_TIMEOUT", 0.05)

    async def hang(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(s3, "upload_file", hang)

    att = _attachment()
    msg = MessageResponse(sender_id="+1", timestamp=TS, attachments=[att])

    # Returns promptly (well under the hung 3600s) and leaves url unset.
    asyncio.run(s3.store_inbound_attachments(msg))

    assert att.url is None


def test_get_client_sets_bounded_timeouts(monkeypatch):
    """The boto3 client caps connect/read time and retries (no minutes-long hang)."""
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    client = s3.get_client()
    assert client.meta.config.connect_timeout == s3._CONNECT_TIMEOUT
    assert client.meta.config.read_timeout == s3._READ_TIMEOUT
    assert client.meta.config.retries["mode"] == "standard"
    # botocore resolves max_attempts (retries) to total_max_attempts = N + 1.
    assert client.meta.config.retries["total_max_attempts"] == s3._MAX_ATTEMPTS + 1


def test_store_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(config, "s3_bucket", "")
    att = _attachment()
    msg = MessageResponse(sender_id="+1", timestamp=TS, attachments=[att])

    asyncio.run(s3.store_inbound_attachments(msg))

    assert att.url is None


def test_store_skips_attachment_without_local_file(monkeypatch):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    called = False

    async def fake_upload(*args, **kwargs):
        nonlocal called
        called = True
        return "key"

    monkeypatch.setattr(s3, "upload_file", fake_upload)
    att = _attachment(path=None)
    msg = MessageResponse(sender_id="+1", timestamp=TS, attachments=[att])

    asyncio.run(s3.store_inbound_attachments(msg))

    assert called is False
    assert att.url is None


# ---------------------------------------------------------------------------
# Channel annotation prefers the URL
# ---------------------------------------------------------------------------


def test_attachment_line_prefers_url_over_path():
    att = _attachment(url="https://cdn.example/k?sig=1")
    assert _attachment_line(att) == (
        "[attachment: https://cdn.example/k?sig=1 (image/png, 245 KB)]"
    )


# ---------------------------------------------------------------------------
# Forwarder wiring
# ---------------------------------------------------------------------------


class _FakeWriteStream:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


class _FakeClient:
    def __init__(self, messages):
        self._messages = list(messages)
        self.calls = []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        return {"timestamp": 1}

    async def next_message(self, timeout):
        if not self._messages:
            await asyncio.sleep(3600)
        return self._messages.pop(0)


def _run_forwarder(messages):
    fake = _FakeClient(messages)
    stream = _FakeWriteStream()

    async def _runner():
        with patch.object(rpc, "client", fake):
            task = asyncio.create_task(_forward_channel_messages(stream))
            await asyncio.sleep(0.15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return stream.sent, fake

    return asyncio.run(_runner())


def test_forwarder_content_carries_presigned_url(monkeypatch):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    monkeypatch.setattr(config, "s3_prefix", "signal-mcp/")
    monkeypatch.setattr(config, "prefix", "")

    async def fake_upload(path, key, content_type):
        return s3.object_key(key)

    async def fake_presign(key, ttl=None):
        return f"https://cdn.example/{s3.object_key(key)}?sig=1"

    monkeypatch.setattr(s3, "upload_file", fake_upload)
    monkeypatch.setattr(s3, "presign", fake_presign)

    att = _attachment()
    msg = MessageResponse(
        message="look", sender_id="+15551234567", timestamp=TS, attachments=[att]
    )
    sent, _ = _run_forwarder([msg])

    assert len(sent) == 1
    content = sent[0].root.params["content"]
    assert content.startswith("look\n[attachment: https://cdn.example/")
    assert content.endswith("(image/png, 245 KB)]")


def test_forwarder_falls_back_and_still_delivers_on_upload_failure(monkeypatch):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    monkeypatch.setattr(config, "prefix", "")

    async def boom(*args, **kwargs):
        raise s3.S3Error("s3 down")

    monkeypatch.setattr(s3, "upload_file", boom)

    att = _attachment()
    msg = MessageResponse(
        message="look", sender_id="+15551234567", timestamp=TS, attachments=[att]
    )
    sent, fake = _run_forwarder([msg])

    assert len(sent) == 1  # message NOT dropped by the S3 failure
    content = sent[0].root.params["content"]
    assert content == (
        "look\n[attachment: /tmp/attachments/abc123.png (image/png, 245 KB)]"
    )
    # The read receipt is still sent.
    assert any(c[0] == "sendReceipt" for c in fake.calls)


def test_forwarder_no_upload_when_s3_disabled(monkeypatch):
    monkeypatch.setattr(config, "s3_bucket", "")
    monkeypatch.setattr(config, "prefix", "")
    called = False

    async def fake_upload(*args, **kwargs):
        nonlocal called
        called = True
        return "key"

    monkeypatch.setattr(s3, "upload_file", fake_upload)

    att = _attachment()
    msg = MessageResponse(
        message="look", sender_id="+15551234567", timestamp=TS, attachments=[att]
    )
    sent, _ = _run_forwarder([msg])

    assert called is False
    assert sent[0].root.params["content"] == (
        "look\n[attachment: /tmp/attachments/abc123.png (image/png, 245 KB)]"
    )
