"""Tests for outbound attachments on the send tools (#17).

Covers _validate_attachments, the "attachments" param in the daemon send
params, attachment-only sends, and the ordering guarantee that the
trusted-recipient check runs before any file validation or RPC.
"""

import asyncio

import pytest

from signal_mcp import rpc, tools
from signal_mcp.config import config
from signal_mcp.rpc import SignalError, UntrustedRecipientError
from signal_mcp.tools import (
    _validate_attachments,
    send,
    send_message_to_group,
    send_message_to_user,
)

ALICE = "+15555550101"
MALLORY = "+15555550199"
GROUP = "GID=="
GROUP_NAME = "#talk-homelab"
DATA_URI = "data:image/png;filename=pixel.png;base64,iVBORw0KGgo="


class FakeClient:
    """Stand-in for SignalRpcClient that records JSON-RPC calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        if method == "listGroups":
            return [{"id": GROUP, "name": GROUP_NAME}]
        return {"timestamp": 1}


@pytest.fixture
def fake(monkeypatch):
    """Install a FakeClient, disable the allowlist, and set a user_id."""
    client = FakeClient()
    monkeypatch.setattr(rpc, "client", client)
    monkeypatch.setattr(config, "trusted_recipients", frozenset())
    monkeypatch.setattr(config, "user_id", ALICE)
    return client


def _send_params(client: FakeClient) -> dict:
    """Return the params of the single ``send`` RPC issued, asserting there is one."""
    sends = [(m, p) for m, p in client.calls if m == "send"]
    assert len(sends) == 1
    return sends[0][1]


# ---------------------------------------------------------------------------
# _validate_attachments helper tests
# ---------------------------------------------------------------------------


def test_validate_attachments_none_and_empty():
    assert _validate_attachments(None) is None
    assert _validate_attachments([]) is None


def test_validate_attachments_data_uri_passthrough(tmp_path):
    """data: URIs pass through completely unchanged (no path resolution)."""
    assert _validate_attachments([DATA_URI]) == [DATA_URI]


def test_validate_attachments_resolves_existing_file(tmp_path, monkeypatch):
    f = tmp_path / "photo.png"
    f.write_bytes(b"png")
    monkeypatch.chdir(tmp_path)

    # A relative path is resolved to an absolute one.
    assert _validate_attachments(["photo.png"]) == [str(f.resolve())]


def test_validate_attachments_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"pdf")

    assert _validate_attachments(["~/doc.pdf"]) == [str(f.resolve())]


def test_validate_attachments_missing_file_raises(tmp_path):
    missing = str(tmp_path / "nope.png")
    with pytest.raises(SignalError, match="not an existing file"):
        _validate_attachments([missing])


def test_validate_attachments_directory_raises(tmp_path):
    with pytest.raises(SignalError, match="not an existing file"):
        _validate_attachments([str(tmp_path)])


def test_validate_attachments_mixed_entries(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hi")

    result = _validate_attachments([DATA_URI, str(f)])
    assert result == [DATA_URI, str(f.resolve())]


# ---------------------------------------------------------------------------
# Param building via the send tools (FakeClient)
# ---------------------------------------------------------------------------


def test_send_message_to_user_includes_attachments(fake, tmp_path):
    f = tmp_path / "photo.png"
    f.write_bytes(b"png")

    result = asyncio.run(send_message_to_user("look", ALICE, [str(f)]))

    assert result == {"message": "Message sent successfully"}
    params = _send_params(fake)
    assert params["message"] == "look"
    assert params["recipient"] == [ALICE]
    assert params["attachments"] == [str(f.resolve())]


def test_send_message_to_user_without_attachments_omits_key(fake):
    asyncio.run(send_message_to_user("hi", ALICE))
    assert "attachments" not in _send_params(fake)


def test_send_message_to_user_attachment_only(fake, tmp_path):
    """An empty message with attachments is a valid send."""
    f = tmp_path / "photo.png"
    f.write_bytes(b"png")

    result = asyncio.run(send_message_to_user("", ALICE, [str(f)]))

    assert result == {"message": "Message sent successfully"}
    params = _send_params(fake)
    assert params["message"] == ""
    assert params["attachments"] == [str(f.resolve())]


def test_send_message_to_user_data_uri_passthrough(fake):
    asyncio.run(send_message_to_user("pic", ALICE, [DATA_URI]))
    assert _send_params(fake)["attachments"] == [DATA_URI]


def test_send_message_to_user_invalid_path_raises_before_rpc(fake, tmp_path):
    """Path validation failure errors out before any RPC is issued."""
    with pytest.raises(SignalError, match="not an existing file"):
        asyncio.run(send_message_to_user("hi", ALICE, [str(tmp_path / "nope.png")]))

    assert fake.calls == []


def test_send_message_to_group_includes_attachments(fake, tmp_path):
    f = tmp_path / "photo.png"
    f.write_bytes(b"png")

    result = asyncio.run(send_message_to_group("look", GROUP_NAME, [str(f)]))

    assert result == {"message": "Message sent successfully"}
    params = _send_params(fake)
    assert params["groupId"] == GROUP
    assert params["attachments"] == [str(f.resolve())]


def test_send_message_to_group_invalid_path_raises_before_send(fake, tmp_path):
    with pytest.raises(SignalError, match="not an existing file"):
        asyncio.run(send_message_to_group("hi", GROUP, [str(tmp_path / "nope.png")]))

    assert all(method != "send" for method, _ in fake.calls)


def test_send_includes_attachments(fake, tmp_path):
    f = tmp_path / "photo.png"
    f.write_bytes(b"png")

    result = asyncio.run(send("look", [str(f)]))

    assert result == {"message": "Message sent successfully"}
    params = _send_params(fake)
    assert params["recipient"] == [ALICE]
    assert params["attachments"] == [str(f.resolve())]


def test_send_invalid_path_raises_before_rpc(fake, tmp_path):
    with pytest.raises(SignalError, match="not an existing file"):
        asyncio.run(send("hi", [str(tmp_path / "nope.png")]))

    assert fake.calls == []


# ---------------------------------------------------------------------------
# Trusted-recipient check runs before attachment validation
# ---------------------------------------------------------------------------


def test_untrusted_user_blocks_before_attachment_validation(fake, monkeypatch):
    """Untrusted recipient + attachments: no file validation, no RPC."""
    monkeypatch.setattr(config, "trusted_recipients", frozenset({ALICE}))
    validated = False

    def spy(attachments):
        nonlocal validated
        validated = True
        return attachments

    monkeypatch.setattr(tools, "_validate_attachments", spy)

    with pytest.raises(UntrustedRecipientError):
        asyncio.run(send_message_to_user("hi", MALLORY, ["/does/not/exist.png"]))

    assert validated is False
    assert fake.calls == []


def test_untrusted_group_blocks_before_attachment_validation(fake, monkeypatch):
    monkeypatch.setattr(config, "trusted_recipients", frozenset({"OTHER_GID=="}))
    validated = False

    def spy(attachments):
        nonlocal validated
        validated = True
        return attachments

    monkeypatch.setattr(tools, "_validate_attachments", spy)

    with pytest.raises(UntrustedRecipientError):
        asyncio.run(send_message_to_group("hi", GROUP, ["/does/not/exist.png"]))

    assert validated is False
    assert all(method != "send" for method, _ in fake.calls)
