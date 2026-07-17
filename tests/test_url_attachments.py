"""Tests for outbound attachments from HTTP(S) URLs (#22).

Driven against a real local HTTP fixture (no mocking of urllib): happy-path
download + send, data-URI transfer mode, the size-cap abort, bad-scheme
rejection, download failures, filename/Content-Type resolution, and temp-file
cleanup after both success and failure.
"""

import asyncio
import base64
import glob
import http.server
import os
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from signal_mcp import rpc, tools
from signal_mcp.config import DEFAULT_ATTACHMENT_MAX_BYTES, config
from signal_mcp.rpc import SignalError
from signal_mcp.tools import (
    _filename_from_content_disposition,
    _is_http_url,
    _resolve_download_name,
    send_message_to_user,
)

ALICE = "+15555550101"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        entry = self.server.routes.get(self.path)  # type: ignore[attr-defined]
        if entry is None:
            self.send_response(404)
            self.end_headers()
            return
        status, headers, body = entry
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence the default stderr logging
        pass


@pytest.fixture
def http_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server.routes = {}  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield server, base
    finally:
        server.shutdown()
        server.server_close()


class RecordingClient:
    """Records JSON-RPC calls and snapshots any local attachment files' bytes.

    Snapshotting during the ``send`` call mirrors signal-cli reading the file
    while it still exists, so a test can assert both the delivered content and
    that the temp file is cleaned up afterward.
    """

    def __init__(self):
        self.calls = []
        self.attachment_bytes = []
        self.attachment_paths = []

    async def call(self, method, params=None, timeout=30.0):
        params = params or {}
        self.calls.append((method, params))
        if method == "send":
            for entry in params.get("attachments", []):
                if not entry.startswith("data:") and os.path.exists(entry):
                    self.attachment_paths.append(entry)
                    self.attachment_bytes.append(Path(entry).read_bytes())
        return {"timestamp": 1}


@pytest.fixture
def fake(monkeypatch):
    client = RecordingClient()
    monkeypatch.setattr(rpc, "client", client)
    monkeypatch.setattr(config, "trusted_recipients", frozenset())
    monkeypatch.setattr(config, "user_id", ALICE)
    monkeypatch.setattr(config, "attachment_transfer", "path")
    monkeypatch.setattr(config, "attachment_max_bytes", DEFAULT_ATTACHMENT_MAX_BYTES)
    monkeypatch.setattr(config, "rpc_host", "127.0.0.1")
    return client


def _send_attachments(client):
    sends = [p for m, p in client.calls if m == "send"]
    assert len(sends) == 1
    return sends[0].get("attachments", [])


def _leftover_temp_dirs():
    return glob.glob(os.path.join(tempfile.gettempdir(), "signal-mcp-att-*"))


# ---------------------------------------------------------------------------
# Classification / helper units
# ---------------------------------------------------------------------------


def test_is_http_url():
    assert _is_http_url("http://x/y") is True
    assert _is_http_url("https://x/y") is True
    assert _is_http_url("ftp://x/y") is False
    assert _is_http_url("/local/path") is False
    assert _is_http_url("data:text/plain;base64,aGk=") is False


def test_filename_from_content_disposition():
    assert (
        _filename_from_content_disposition('attachment; filename="report.pdf"')
        == "report.pdf"
    )
    # RFC 2231 extended form
    assert (
        _filename_from_content_disposition("attachment; filename*=UTF-8''caf%C3%A9.png")
        == "café.png"
    )
    # Path components are stripped to a basename (no smuggling).
    assert (
        _filename_from_content_disposition('attachment; filename="/etc/passwd"')
        == "passwd"
    )
    assert _filename_from_content_disposition(None) is None


def test_resolve_download_name_adds_extension_from_content_type():
    class H(dict):
        pass

    # No Content-Disposition, URL basename has no extension -> derive from type.
    name = _resolve_download_name(H(), "http://h/download", "image/png")
    assert name.endswith(".png")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_url_download_and_send_path_mode(fake, http_server):
    server, base = http_server
    server.routes["/pic"] = (
        200,
        {
            "Content-Type": "image/png",
            "Content-Disposition": 'inline; filename="p.png"',
        },
        PNG,
    )

    result = asyncio.run(send_message_to_user("look", ALICE, [f"{base}/pic"]))
    assert result == {"message": "Message sent successfully"}

    # The daemon received a local temp path whose bytes are the downloaded file.
    attachments = _send_attachments(fake)
    assert len(attachments) == 1
    assert not attachments[0].startswith("data:")
    assert os.path.basename(attachments[0]) == "p.png"
    assert fake.attachment_bytes == [PNG]
    # ...and the temp file is cleaned up after the send.
    assert not os.path.exists(fake.attachment_paths[0])
    assert _leftover_temp_dirs() == []


def test_url_download_data_uri_mode(fake, monkeypatch, http_server):
    monkeypatch.setattr(config, "attachment_transfer", "data-uri")
    server, base = http_server
    server.routes["/pic"] = (200, {"Content-Type": "image/png"}, PNG)

    asyncio.run(send_message_to_user("look", ALICE, [f"{base}/pic"]))

    (uri,) = _send_attachments(fake)
    header, b64 = uri.split(",", 1)
    # Content-Type comes from the response header, not an extension guess.
    assert header.startswith("data:image/png;filename=")
    assert base64.b64decode(b64) == PNG
    assert _leftover_temp_dirs() == []


def test_url_basename_used_when_no_content_disposition(fake, http_server):
    server, base = http_server
    server.routes["/files/photo.png"] = (200, {"Content-Type": "image/png"}, PNG)

    asyncio.run(send_message_to_user("", ALICE, [f"{base}/files/photo.png"]))

    (path,) = _send_attachments(fake)
    assert os.path.basename(path) == "photo.png"


# ---------------------------------------------------------------------------
# Size cap, bad scheme, download failures — all abort before any RPC
# ---------------------------------------------------------------------------


def test_url_size_cap_aborts_before_rpc(fake, monkeypatch, http_server):
    monkeypatch.setattr(config, "attachment_max_bytes", 10)
    server, base = http_server
    server.routes["/big"] = (
        200,
        {"Content-Type": "application/octet-stream"},
        b"x" * 50,
    )

    with pytest.raises(SignalError, match="exceeds"):
        asyncio.run(send_message_to_user("hi", ALICE, [f"{base}/big"]))

    assert fake.calls == []  # no RPC issued
    assert _leftover_temp_dirs() == []  # temp dir cleaned up despite the abort


def test_bad_url_scheme_rejected_before_rpc(fake):
    with pytest.raises(SignalError, match="only http and https"):
        asyncio.run(send_message_to_user("hi", ALICE, ["ftp://host/file.bin"]))
    assert fake.calls == []


def test_url_download_404_errors_before_rpc(fake, http_server):
    server, base = http_server
    with pytest.raises(SignalError, match="Failed to download"):
        asyncio.run(send_message_to_user("hi", ALICE, [f"{base}/missing"]))
    assert fake.calls == []
    assert _leftover_temp_dirs() == []


def test_url_connection_refused_errors_before_rpc(fake):
    # Nothing listening on this port -> connection refused -> SignalError.
    with pytest.raises(SignalError, match="Failed to download"):
        asyncio.run(send_message_to_user("hi", ALICE, ["http://127.0.0.1:9/none"]))
    assert fake.calls == []
    assert _leftover_temp_dirs() == []


# ---------------------------------------------------------------------------
# Redirects: followed only to http(s) targets
# ---------------------------------------------------------------------------


def test_url_redirect_to_http_is_followed(fake, http_server):
    server, base = http_server
    server.routes["/pic"] = (200, {"Content-Type": "image/png"}, PNG)
    server.routes["/redir"] = (302, {"Location": f"{base}/pic"}, b"")

    asyncio.run(send_message_to_user("look", ALICE, [f"{base}/redir"]))

    assert fake.attachment_bytes == [PNG]
    assert _leftover_temp_dirs() == []


def test_url_redirect_to_non_http_scheme_rejected(fake, http_server):
    """A redirect to a non-http(s) scheme (e.g. ftp) is refused, not followed."""
    server, base = http_server
    server.routes["/evil"] = (302, {"Location": "ftp://example.com/secret"}, b"")

    with pytest.raises(SignalError, match="redirect"):
        asyncio.run(send_message_to_user("hi", ALICE, [f"{base}/evil"]))

    assert fake.calls == []
    assert _leftover_temp_dirs() == []


# ---------------------------------------------------------------------------
# Cancellation-safe temp dir: registered for cleanup before the worker thread
# ---------------------------------------------------------------------------


def test_download_url_preregisters_temp_dir_before_thread(monkeypatch):
    """The temp dir is created and registered for cleanup BEFORE the blocking
    download runs, so a cancelled download cannot orphan an unregistered dir."""

    async def scenario():
        cleanup: list[Path] = []
        captured: dict[str, Path] = {}

        def fake_blocking(url, tmp_dir):
            captured["dir"] = tmp_dir
            assert tmp_dir in cleanup  # already registered before we run
            return tmp_dir / "f.bin", "application/octet-stream"

        monkeypatch.setattr(tools, "_download_url_blocking", fake_blocking)
        path, _ = await tools._download_url("http://host/f", cleanup)
        assert captured["dir"] in cleanup
        assert path.parent == captured["dir"]
        for tmp_dir in cleanup:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Mixed entries: a URL alongside a local file and a data: URI
# ---------------------------------------------------------------------------


def test_mixed_url_file_and_data_uri(fake, tmp_path, http_server):
    server, base = http_server
    server.routes["/pic"] = (200, {"Content-Type": "image/png"}, PNG)
    local = tmp_path / "local.txt"
    local.write_text("hello")
    data_uri = "data:text/plain;filename=x.txt;base64,aGk="

    asyncio.run(
        send_message_to_user("all", ALICE, [f"{base}/pic", str(local), data_uri])
    )

    url_att, file_att, data_att = _send_attachments(fake)
    assert os.path.basename(url_att) == "pic.png"  # ext derived from Content-Type
    assert file_att == str(local.resolve())
    assert data_att == data_uri
    assert _leftover_temp_dirs() == []
