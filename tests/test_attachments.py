"""Tests for outbound attachments on the send tools (#17, #18).

Covers _validate_attachments, the "attachments" param in the daemon send
params, attachment-only sends, the ordering guarantee that the
trusted-recipient check runs before any file validation or RPC, and the
data-URI transfer fallback for remote daemons: auto-mode loopback detection,
explicit mode overrides, RFC 2397 encoding round-trips, the size cap, and
data: passthrough in every mode.
"""

import asyncio
import base64
import dataclasses
import os
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

from signal_mcp import rpc, tools
from signal_mcp.config import (
    DEFAULT_ATTACHMENT_MAX_BYTES,
    SignalConfig,
    config,
    parse_args,
)
from signal_mcp.rpc import SignalError, UntrustedRecipientError
from signal_mcp.tools import (
    _encode_data_uri,
    _is_loopback_host,
    _resolve_transfer_mode,
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
    """Install a FakeClient, disable the allowlist, and set an operator.

    Attachment transfer settings are pinned to their defaults (auto mode
    against a loopback daemon, 25 MB cap) so tests are hermetic regardless of
    the ambient global config.
    """
    client = FakeClient()
    monkeypatch.setattr(rpc, "client", client)
    monkeypatch.setattr(config, "trusted_recipients", frozenset())
    monkeypatch.setattr(config, "operator", ALICE)
    monkeypatch.setattr(config, "attachment_transfer", "auto")
    monkeypatch.setattr(config, "attachment_max_bytes", DEFAULT_ATTACHMENT_MAX_BYTES)
    monkeypatch.setattr(config, "rpc_host", "127.0.0.1")
    return client


@pytest.fixture
def clean_config(monkeypatch):
    """Snapshot the global config and clear SIGNAL_MCP_* env vars.

    parse_args mutates the module-global config in place; snapshotting every
    field lets monkeypatch restore the pristine values on teardown so other
    tests are unaffected.
    """
    for f in dataclasses.fields(SignalConfig):
        monkeypatch.setattr(config, f.name, getattr(config, f.name))
    for key in [k for k in os.environ if k.startswith("SIGNAL_MCP_")]:
        monkeypatch.delenv(key)


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


# ---------------------------------------------------------------------------
# Transfer mode resolution (#18): auto-mode loopback detection and overrides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.8.9.1", "::1", "[::1]", "localhost", "LOCALHOST"],
)
def test_auto_mode_loopback_host_resolves_to_path(monkeypatch, host):
    monkeypatch.setattr(config, "attachment_transfer", "auto")
    monkeypatch.setattr(config, "rpc_host", host)
    assert _resolve_transfer_mode() == "path"


@pytest.mark.parametrize(
    "host",
    ["192.168.1.50", "10.0.0.7", "signal.example.com", "2001:db8::1"],
)
def test_auto_mode_remote_host_resolves_to_data_uri(monkeypatch, host):
    monkeypatch.setattr(config, "attachment_transfer", "auto")
    monkeypatch.setattr(config, "rpc_host", host)
    assert _resolve_transfer_mode() == "data-uri"


def test_explicit_path_mode_overrides_remote_host(monkeypatch):
    monkeypatch.setattr(config, "attachment_transfer", "path")
    monkeypatch.setattr(config, "rpc_host", "192.168.1.50")
    assert _resolve_transfer_mode() == "path"


def test_explicit_data_uri_mode_overrides_loopback_host(monkeypatch):
    monkeypatch.setattr(config, "attachment_transfer", "data-uri")
    monkeypatch.setattr(config, "rpc_host", "127.0.0.1")
    assert _resolve_transfer_mode() == "data-uri"


def test_is_loopback_host_rejects_non_loopback_forms():
    assert _is_loopback_host("128.0.0.1") is False
    assert _is_loopback_host("localhost.example.com") is False
    assert _is_loopback_host("") is False


# ---------------------------------------------------------------------------
# Data-URI encoding (#18): round-trip, MIME fallback, cap enforcement
# ---------------------------------------------------------------------------


def test_data_uri_encode_round_trip(fake, monkeypatch, tmp_path):
    """The produced URI decodes back to the original bytes, MIME, and name."""
    monkeypatch.setattr(config, "attachment_transfer", "data-uri")
    payload = bytes(range(256))
    f = tmp_path / "photo.png"
    f.write_bytes(payload)

    asyncio.run(send_message_to_user("pic", ALICE, [str(f)]))

    (uri,) = _send_params(fake)["attachments"]
    header, b64data = uri.split(",", 1)
    assert header == "data:image/png;filename=photo.png;base64"
    assert base64.b64decode(b64data) == payload


def test_data_uri_unknown_extension_falls_back_to_octet_stream(
    fake, monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "attachment_transfer", "data-uri")
    f = tmp_path / "blob.zzz987"
    f.write_bytes(b"opaque")

    asyncio.run(send_message_to_user("blob", ALICE, [str(f)]))

    (uri,) = _send_params(fake)["attachments"]
    assert uri.startswith("data:application/octet-stream;filename=blob.zzz987;base64,")


def test_auto_mode_remote_host_encodes_file(fake, monkeypatch, tmp_path):
    """auto + non-loopback daemon: local files go out as data URIs."""
    monkeypatch.setattr(config, "rpc_host", "192.168.1.50")
    f = tmp_path / "note.txt"
    f.write_text("hello")

    asyncio.run(send("here", [str(f)]))

    (uri,) = _send_params(fake)["attachments"]
    assert uri.startswith("data:text/plain;filename=note.txt;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == b"hello"


def test_explicit_path_mode_sends_path_to_remote_host(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "attachment_transfer", "path")
    monkeypatch.setattr(config, "rpc_host", "192.168.1.50")
    f = tmp_path / "photo.png"
    f.write_bytes(b"png")

    asyncio.run(send_message_to_user("look", ALICE, [str(f)]))

    assert _send_params(fake)["attachments"] == [str(f.resolve())]


def test_cap_exceeded_raises_before_rpc(fake, monkeypatch, tmp_path):
    """Oversized file in data-uri mode: actionable error, no RPC issued."""
    monkeypatch.setattr(config, "attachment_transfer", "data-uri")
    monkeypatch.setattr(config, "attachment_max_bytes", 10)
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 11)

    with pytest.raises(SignalError) as exc:
        asyncio.run(send_message_to_user("hi", ALICE, [str(f)]))

    message = str(exc.value)
    assert "big.bin" in message
    assert "11" in message
    assert "10" in message
    assert "--attachment-max-bytes" in message
    assert fake.calls == []


def test_cap_boundary_file_is_allowed(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "attachment_transfer", "data-uri")
    monkeypatch.setattr(config, "attachment_max_bytes", 10)
    f = tmp_path / "ok.bin"
    f.write_bytes(b"x" * 10)

    asyncio.run(send_message_to_user("hi", ALICE, [str(f)]))

    (uri,) = _send_params(fake)["attachments"]
    assert base64.b64decode(uri.split(",", 1)[1]) == b"x" * 10


def test_cap_not_enforced_in_path_mode(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "attachment_transfer", "path")
    monkeypatch.setattr(config, "attachment_max_bytes", 10)
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 100)

    asyncio.run(send_message_to_user("hi", ALICE, [str(f)]))

    assert _send_params(fake)["attachments"] == [str(f.resolve())]


def test_cap_rechecked_after_read(monkeypatch, tmp_path):
    """A file that grows between stat and read still trips the cap (TOCTOU)."""
    monkeypatch.setattr(config, "attachment_max_bytes", 10)
    f = tmp_path / "grow.bin"
    f.write_bytes(b"x" * 5)  # passes the stat-based check
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"x" * 11)

    with pytest.raises(SignalError, match="11 bytes"):
        _encode_data_uri(f)


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("comma,name.txt", "text/plain"),
        ("semi;colon.txt", "text/plain"),
        ("Report, Final.pdf", "application/pdf"),
        ("spa ce.txt", "text/plain"),
        ("per%cent.txt", "text/plain"),
        ('quo"te.txt', "text/plain"),
        ("émoji café.png", "image/png"),
    ],
)
def test_data_uri_hostile_basenames_round_trip(fake, monkeypatch, tmp_path, name, mime):
    """Basenames with ',', ';', spaces, '%', quotes, or non-ASCII stay intact.

    A raw ',' or ';' in the filename parameter would corrupt the URI — RFC
    2397 defines the payload as everything after the *first* comma — so the
    URI is parsed structurally: split at the first comma, then verify each
    header parameter and that the basename percent-decodes back exactly.
    """
    monkeypatch.setattr(config, "attachment_transfer", "data-uri")
    payload = b"\x00hostile\xffbytes"
    f = tmp_path / name
    f.write_bytes(payload)

    asyncio.run(send_message_to_user("f", ALICE, [str(f)]))

    (uri,) = _send_params(fake)["attachments"]
    header, b64data = uri.split(",", 1)
    assert base64.b64decode(b64data, validate=True) == payload

    mime_part, filename_param, base64_marker = header.split(";")
    assert mime_part == f"data:{mime}"
    assert base64_marker == "base64"
    assert filename_param.startswith("filename=")
    encoded_name = filename_param.removeprefix("filename=")
    # Fully percent-encoded: only unreserved characters and % escapes remain.
    assert re.fullmatch(r"[A-Za-z0-9._~%-]+", encoded_name)
    assert unquote(encoded_name) == name


# ---------------------------------------------------------------------------
# data: URI passthrough in every mode (#18)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "host"),
    [
        ("path", "127.0.0.1"),
        ("data-uri", "127.0.0.1"),
        ("auto", "127.0.0.1"),
        ("auto", "192.168.1.50"),
    ],
)
def test_caller_data_uri_passthrough_in_every_mode(fake, monkeypatch, mode, host):
    monkeypatch.setattr(config, "attachment_transfer", mode)
    monkeypatch.setattr(config, "rpc_host", host)

    asyncio.run(send_message_to_user("pic", ALICE, [DATA_URI]))

    assert _send_params(fake)["attachments"] == [DATA_URI]


def test_mixed_entries_in_data_uri_mode(fake, monkeypatch, tmp_path):
    """Caller data: URIs untouched while sibling file entries are encoded."""
    monkeypatch.setattr(config, "attachment_transfer", "data-uri")
    f = tmp_path / "a.txt"
    f.write_text("hi")

    asyncio.run(send_message_to_user("both", ALICE, [DATA_URI, str(f)]))

    first, second = _send_params(fake)["attachments"]
    assert first == DATA_URI
    assert second.startswith("data:text/plain;filename=a.txt;base64,")


# ---------------------------------------------------------------------------
# Config parsing (#18): flags, env vars, validation
# ---------------------------------------------------------------------------


def test_parse_args_attachment_defaults(clean_config):
    cfg = parse_args(["--operator", ALICE])
    assert cfg.attachment_transfer == "auto"
    assert cfg.attachment_max_bytes == DEFAULT_ATTACHMENT_MAX_BYTES


def test_parse_args_attachment_flags(clean_config):
    cfg = parse_args(
        [
            "--operator",
            ALICE,
            "--attachment-transfer",
            "data-uri",
            "--attachment-max-bytes",
            "1024",
        ]
    )
    assert cfg.attachment_transfer == "data-uri"
    assert cfg.attachment_max_bytes == 1024


def test_parse_args_attachment_env(clean_config, monkeypatch):
    monkeypatch.setenv("SIGNAL_MCP_ATTACHMENT_TRANSFER", "path")
    monkeypatch.setenv("SIGNAL_MCP_ATTACHMENT_MAX_BYTES", "2048")
    cfg = parse_args(["--operator", ALICE])
    assert cfg.attachment_transfer == "path"
    assert cfg.attachment_max_bytes == 2048


def test_parse_args_invalid_transfer_errors(clean_config, capsys):
    with pytest.raises(SystemExit):
        parse_args(["--operator", ALICE, "--attachment-transfer", "carrier-pigeon"])
    assert "attachment transfer" in capsys.readouterr().err


def test_parse_args_invalid_transfer_env_errors(clean_config, monkeypatch, capsys):
    monkeypatch.setenv("SIGNAL_MCP_ATTACHMENT_TRANSFER", "carrier-pigeon")
    with pytest.raises(SystemExit):
        parse_args(["--operator", ALICE])
    assert "SIGNAL_MCP_ATTACHMENT_TRANSFER" in capsys.readouterr().err


def test_parse_args_nonpositive_max_bytes_errors(clean_config, capsys):
    with pytest.raises(SystemExit):
        parse_args(["--operator", ALICE, "--attachment-max-bytes", "0"])
    assert "positive" in capsys.readouterr().err
