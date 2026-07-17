"""FastMCP tool definitions and send helpers for the Signal MCP server."""

import asyncio
import base64
import contextlib
import functools
import ipaddress
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ParamSpec, TypeVar
from urllib.parse import quote, urlsplit

from mcp.server.fastmcp import FastMCP

from signal_mcp import s3
from signal_mcp.config import _normalize_recipient, config, is_trusted_sender
from signal_mcp.parse import MessageResponse
from signal_mcp.prompts import register_prompts
from signal_mcp.rpc import (
    SignalCLIError,
    SignalDisconnectedError,
    SignalError,
    UntrustedRecipientError,
    get_client,
)

logger = logging.getLogger(__name__)

# The MCP server instance all tools register against.
mcp = FastMCP(name="signal-cli")
register_prompts(mcp)

P = ParamSpec("P")
T = TypeVar("T")


def _log_tool_errors(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Log tool failures once, then re-raise for FastMCP to report as tool errors.

    FastMCP converts exceptions raised by a tool into a proper tool-error
    result (``isError``), so tools raise instead of returning ``{"error": ...}``
    payloads. This decorator keeps the logging in one place instead of a
    try/except copy in every tool.
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await func(*args, **kwargs)
        except SignalError as e:
            logger.error(f"{func.__name__} failed: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in {func.__name__}: {e}", exc_info=True)
            raise

    return wrapper


def _ensure_trusted(target: str) -> None:
    """Reject sends to recipients that are not on the configured allowlist.

    When no trusted recipients are configured the allowlist is disabled and
    every recipient is permitted, so enforcement is opt-in. The check runs at
    the MCP tool boundary, before any JSON-RPC ``send``/``sendReaction`` is
    issued to the daemon, so the LLM can only message recipients the server
    operator has explicitly approved.
    """
    if not config.trusted_recipients:
        return

    if _normalize_recipient(target) not in config.trusted_recipients:
        logger.warning(f"Blocked send to untrusted recipient: {target}")
        raise UntrustedRecipientError(
            f"Recipient {target} is not in the trusted recipients allowlist"
        )


async def _resolve_group(name_or_id: str) -> dict[str, Any] | None:
    """Find a group by internal id or display name; ``None`` when unknown."""
    logger.info(f"Resolving group: {name_or_id}")
    try:
        groups = await get_client().call("listGroups")
    except SignalCLIError as e:
        logger.error(f"Error listing groups: {e}")
        return None

    for g in groups or []:
        if g.get("id") == name_or_id or g.get("name") == name_or_id:
            return g

    logger.error(f"Could not find group: {name_or_id}")
    return None


async def _ensure_trusted_group(target: str) -> str:
    """Resolve a group and enforce the allowlist; returns the group's id.

    The allowlist may hold the group's internal id *or* its display name, and
    the caller may likewise pass either form — the group is resolved first,
    then both its id and name are checked against the allowlist. When the
    allowlist is non-empty and the group cannot be resolved (so neither form
    can be verified), the send is rejected.
    """
    normalized = _normalize_recipient(target)
    allowlist = config.trusted_recipients
    directly_trusted = not allowlist or normalized in allowlist

    group = await _resolve_group(normalized)
    if group is None:
        if not directly_trusted:
            logger.warning(f"Blocked send to untrusted group: {target}")
            raise UntrustedRecipientError(
                f"Recipient {target} is not in the trusted recipients allowlist"
            )
        raise SignalError(f"Could not find group: {target}")

    gid = group.get("id")
    if not isinstance(gid, str) or not gid:
        raise SignalError(f"Group {target} has no id")

    if not directly_trusted:
        candidates = {c for c in (gid, group.get("name")) if isinstance(c, str)}
        if not candidates & allowlist:
            logger.warning(f"Blocked send to untrusted group: {target}")
            raise UntrustedRecipientError(
                f"Recipient {target} is not in the trusted recipients allowlist"
            )

    logger.info(f"Resolved group {target!r} -> {gid}")
    return gid


async def _send_receipt(sender: str, target_timestamp: float) -> None:
    """Send a read receipt for a received message via the daemon.

    ``target_timestamp`` is coerced to an int (signal-cli expects milliseconds).
    Read receipts are always addressed to the individual author of the message,
    even when it was posted in a group — signal-cli has no ``groupId`` param
    for ``sendReceipt``. Raises :class:`SignalCLIError` on failure.
    """
    params: dict[str, Any] = {
        "recipient": [sender],
        "targetTimestamp": int(target_timestamp),
        "type": "read",
    }
    await get_client().call("sendReceipt", params)
    logger.info(f"Sent read receipt for message from {sender}")


_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
# Connect/read timeout and chunk size for outbound URL attachment downloads.
_URL_TIMEOUT = 30.0
_DOWNLOAD_CHUNK = 65536


def _is_http_url(entry: str) -> bool:
    """True when ``entry`` is an ``http://`` or ``https://`` URL."""
    return bool(_URL_SCHEME_RE.match(entry)) and entry.split("://", 1)[0].lower() in (
        "http",
        "https",
    )


def _validate_attachments(attachments: list[str] | None) -> list[str] | None:
    """Validate outbound attachments before any RPC is issued.

    Entries starting with ``data:`` (RFC 2397 data URIs) pass through
    unchanged. ``http(s)`` URLs pass through too (downloaded later, still
    before any RPC); any other URL scheme is rejected. Everything else must be
    an existing, readable file — ``~`` is expanded and the path is resolved to
    an absolute path. Raises :class:`SignalError` with a clear message on the
    first invalid entry, so the tool errors out before any daemon ``send`` is
    attempted.
    """
    if not attachments:
        return None

    validated: list[str] = []
    for entry in attachments:
        if entry.startswith("data:"):
            validated.append(entry)
            continue

        if _URL_SCHEME_RE.match(entry):
            if not _is_http_url(entry):
                scheme = entry.split("://", 1)[0].lower()
                raise SignalError(
                    f"Unsupported attachment URL scheme {scheme!r} in {entry!r}: "
                    "only http and https URLs are accepted"
                )
            validated.append(entry)  # downloaded later, before any RPC
            continue

        path = Path(entry).expanduser()
        if not path.is_file():
            raise SignalError(f"Attachment is not an existing file: {entry}")
        resolved = path.resolve()
        try:
            with resolved.open("rb"):
                pass
        except OSError as e:
            raise SignalError(f"Attachment is not readable: {entry} ({e})") from e
        validated.append(str(resolved))

    return validated


def _is_loopback_host(host: str) -> bool:
    """True when ``host`` is a loopback address or the ``localhost`` hostname.

    Covers 127.0.0.0/8, ``::1`` (including bracketed IPv6 literals), and the
    ``localhost`` hostname (case-insensitive). Any other hostname or address
    is treated as remote — resolving arbitrary hostnames to decide would add
    DNS lookups to every send, so only the unambiguous forms count.
    """
    hostname = host.strip().strip("[]").lower()
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _resolve_transfer_mode() -> str:
    """Resolve the configured attachment transfer mode to ``path``/``data-uri``.

    ``auto`` becomes ``data-uri`` when the signal-cli daemon is remote (its
    RPC host is not a loopback address), because local file paths would not
    exist on the daemon's filesystem; against a local daemon it stays ``path``.
    Explicit ``path``/``data-uri`` settings are returned as-is.
    """
    mode = config.attachment_transfer
    if mode != "auto":
        return mode
    return "path" if _is_loopback_host(config.rpc_host) else "data-uri"


def _encode_data_uri(path: Path, content_type: str | None = None) -> str:
    """Encode a local file as an RFC 2397 data URI for the daemon.

    Produces ``data:<mime>;filename=<basename>;base64,<data>`` — the MIME type
    is ``content_type`` when given (e.g. the Content-Type of a downloaded URL),
    otherwise guessed from the file extension (``application/octet-stream`` when
    unknown) and the original basename is preserved in the ``filename``
    parameter so the recipient sees a sensible name. The basename is
    percent-encoded (RFC 2397/3986): a raw ``,`` or ``;`` in a filename would
    otherwise corrupt the URI structure, since everything after the *first*
    comma is the payload. Files larger than ``config.attachment_max_bytes``
    raise :class:`SignalError` before any bytes are read; the size is
    re-checked after reading in case the file grew in between.
    """

    def _check_cap(size: int) -> None:
        if size > config.attachment_max_bytes:
            raise SignalError(
                f"Attachment {path} is {size} bytes, which exceeds the "
                f"{config.attachment_max_bytes}-byte limit for data-URI "
                "transfer (raise it with --attachment-max-bytes / "
                "SIGNAL_MCP_ATTACHMENT_MAX_BYTES)"
            )

    _check_cap(path.stat().st_size)
    data = path.read_bytes()
    _check_cap(len(data))
    mime = (
        content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )
    encoded = base64.b64encode(data).decode("ascii")
    filename = quote(path.name, safe="")
    return f"data:{mime};filename={filename};base64,{encoded}"


def _filename_from_content_disposition(header: str | None) -> str | None:
    """Extract a filename from a ``Content-Disposition`` header, or ``None``.

    Handles both ``filename=`` and RFC 2231 ``filename*=`` via the stdlib email
    parser. Only the basename is kept, so a header can never smuggle a path.
    """
    if not header:
        return None
    msg = EmailMessage()
    msg["Content-Disposition"] = header
    name = msg.get_filename()
    return os.path.basename(name) if name else None


def _resolve_download_name(headers: Any, url: str, content_type: str | None) -> str:
    """Pick a safe basename for a file downloaded from ``url``.

    Prefers the ``Content-Disposition`` filename, then the URL path basename,
    falling back to ``download``. Only ever a basename (no directory parts).
    When the name has no extension, one is derived from the response
    Content-Type so signal-cli / data-URI MIME guessing stays correct.
    """
    name = _filename_from_content_disposition(headers.get("Content-Disposition"))
    if not name:
        name = os.path.basename(urlsplit(url).path)
    name = os.path.basename(name)
    if not name or name in (".", ".."):
        name = "download"
    if content_type and not os.path.splitext(name)[1]:
        ext = mimetypes.guess_extension(content_type)
        if ext:
            name += ext
    return name


class _HTTPSchemeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to http(s) targets.

    urllib's default handler also permits ``ftp://`` redirect targets, which
    would let a validated http(s) URL bounce to a scheme the allowlist rejects.
    Enforcing the allowlist on every hop keeps the http(s)-only guarantee across
    redirects. (The internal-host SSRF surface — a redirect to a private/
    link-local http host — remains bounded by network policy; see the README.)
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not _is_http_url(newurl):
            raise SignalError(
                f"Attachment URL redirected to a non-http(s) target: {newurl!r}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_url_opener = urllib.request.build_opener(_HTTPSchemeRedirectHandler())


def _download_url_blocking(url: str, tmp_dir: Path) -> tuple[Path, str | None]:
    """Download ``url`` into ``tmp_dir``; return ``(path, content_type)``.

    ``tmp_dir`` is created and registered for cleanup by :func:`_download_url`
    *before* this runs in a worker thread, so a cancelled download can never
    orphan an unregistered temp directory. Streams the body with a running size
    check against ``config.attachment_max_bytes`` and raises :class:`SignalError`
    on any failure (bad status, timeout, disallowed redirect, oversize) so the
    tool errors before any RPC.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "signal-mcp"})
    try:
        response = _url_opener.open(request, timeout=_URL_TIMEOUT)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SignalError(f"Failed to download attachment URL {url!r}: {e}") from e

    with response:
        raw_type = response.headers.get("Content-Type")
        content_type = raw_type.split(";")[0].strip().lower() if raw_type else None
        name = _resolve_download_name(response.headers, url, content_type)
        dest = tmp_dir / name
        total = 0
        try:
            with dest.open("wb") as out:
                while True:
                    chunk = response.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > config.attachment_max_bytes:
                        raise SignalError(
                            f"Attachment URL {url!r} exceeds the "
                            f"{config.attachment_max_bytes}-byte limit "
                            "(--attachment-max-bytes / "
                            "SIGNAL_MCP_ATTACHMENT_MAX_BYTES)"
                        )
                    out.write(chunk)
        except OSError as e:
            raise SignalError(f"Failed to save attachment from {url!r}: {e}") from e
    return dest, content_type


async def _download_url(url: str, cleanup: list[Path]) -> tuple[Path, str | None]:
    """Download ``url`` off the event loop into a pre-registered temp dir.

    The temp directory is created and appended to ``cleanup`` *before* the
    blocking download is handed to a worker thread. asyncio cannot cancel the
    thread itself, so if the awaiting coroutine is cancelled mid-download the
    registered directory is still removed by :func:`_prepared_attachments`
    (rather than leaking an unregistered dir the thread created).
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="signal-mcp-att-"))
    cleanup.append(tmp_dir)
    return await asyncio.to_thread(_download_url_blocking, url, tmp_dir)


async def _prepare_attachments(
    attachments: list[str] | None, cleanup: list[Path]
) -> list[str] | None:
    """Validate outbound attachments, download URLs, and apply the transfer mode.

    Runs :func:`_validate_attachments` first (rejecting missing files and
    non-http(s) URL schemes before any RPC). Each ``http(s)`` entry is then
    downloaded to a temp file (its dir appended to ``cleanup`` for removal by
    :func:`_prepared_attachments`) and treated like any local file thereafter.
    Finally, when the transfer mode resolves to ``data-uri``, every non-``data:``
    entry is encoded as an RFC 2397 data URI — a downloaded file carrying its
    response Content-Type. Caller ``data:`` URIs pass through unchanged in every
    mode. Raises :class:`SignalError` before any RPC on a validation/download
    failure or a file over the size cap.
    """
    validated = _validate_attachments(attachments)
    if not validated:
        return None

    as_data_uri = _resolve_transfer_mode() == "data-uri"
    prepared: list[str] = []
    for entry in validated:
        if entry.startswith("data:"):
            prepared.append(entry)
        elif _is_http_url(entry):
            path, content_type = await _download_url(entry, cleanup)
            prepared.append(
                _encode_data_uri(path, content_type) if as_data_uri else str(path)
            )
        else:  # a validated local file path
            prepared.append(_encode_data_uri(Path(entry)) if as_data_uri else entry)
    return prepared


@contextlib.asynccontextmanager
async def _prepared_attachments(
    attachments: list[str] | None,
) -> AsyncIterator[list[str] | None]:
    """Prepare outbound attachments, cleaning up any temp downloads afterward.

    Yields the daemon-ready ``attachments`` list (local paths and/or data URIs)
    for the duration of the send, then removes any temp directories created for
    downloaded http(s) URLs — whether the send succeeded or raised.
    """
    cleanup: list[Path] = []
    try:
        yield await _prepare_attachments(attachments, cleanup)
    finally:
        for tmp_dir in cleanup:
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _send_message(
    message: str,
    target: str,
    is_group: bool = False,
    attachments: list[str] | None = None,
) -> None:
    """Send a message to either a user or group via the daemon.

    ``attachments`` entries must already be prepared (see
    :func:`_prepare_attachments`); they are passed to the daemon as the
    ``"attachments"`` array. The message text may be empty when attachments
    are present. Raises :class:`SignalCLIError` on failure.
    """
    target_type = "group" if is_group else "user"
    params: dict[str, Any] = {"message": message}
    if attachments:
        params["attachments"] = attachments
    if is_group:
        params["groupId"] = target
    else:
        params["recipient"] = [target]

    await get_client().call("send", params)
    logger.info(f"Sent message to {target_type}: {target}")


async def _send_reaction(
    emoji: str,
    target: str,
    target_author: str,
    target_timestamp: int,
    is_group: bool = False,
    remove: bool = False,
) -> None:
    """Send (or remove) an emoji reaction to a message via the daemon.

    ``target`` is the recipient (a user number or a group id). ``target_author``
    and ``target_timestamp`` identify the message being reacted to. Raises
    :class:`SignalCLIError` on failure.
    """
    target_type = "group" if is_group else "user"
    params: dict[str, Any] = {
        "emoji": emoji,
        "targetAuthor": target_author,
        "targetTimestamp": int(target_timestamp),
        "remove": remove,
    }
    if is_group:
        params["groupId"] = target
    else:
        params["recipient"] = [target]

    await get_client().call("sendReaction", params)
    action = "Removed" if remove else "Sent"
    logger.info(f"{action} reaction {emoji!r} to {target_type}: {target}")


@mcp.tool()
@_log_tool_errors
async def send_message_to_user(
    message: str, user_id: str, attachments: list[str] | None = None
) -> dict[str, str]:
    """Send a message to a specific user using signal-cli.

    ``attachments`` is an optional list of file paths, ``http(s)`` URLs, or RFC
    2397 ``data:`` URIs (``data:<MIME>;filename=<NAME>;base64,<DATA>``). File
    paths are read on this server's host; URLs are downloaded by the server;
    when the daemon is remote, files are embedded as data URIs automatically.
    ``message`` may be empty when attachments are provided.
    """
    logger.info(f"Tool called: send_message_to_user for user {user_id}")
    _ensure_trusted(user_id)
    async with _prepared_attachments(attachments) as files:
        await _send_message(message, user_id, is_group=False, attachments=files)
    return {"message": "Message sent successfully"}


@mcp.tool()
@_log_tool_errors
async def send_message_to_group(
    message: str, group_id: str, attachments: list[str] | None = None
) -> dict[str, str]:
    """Send a message to a group using signal-cli.

    ``group_id`` may be the group's internal id (the ``group_id`` returned by
    receive_message) or its display name. ``attachments`` is an optional list
    of file paths (read on this server's host; embedded as data URIs when the
    daemon is remote), ``http(s)`` URLs (downloaded by the server), or RFC 2397
    ``data:`` URIs; ``message`` may be empty when attachments are provided.
    """
    logger.info(f"Tool called: send_message_to_group for group {group_id}")
    gid = await _ensure_trusted_group(group_id)
    async with _prepared_attachments(attachments) as files:
        await _send_message(message, gid, is_group=True, attachments=files)
    return {"message": "Message sent successfully"}


@mcp.tool()
@_log_tool_errors
async def send_reaction_to_user(
    emoji: str,
    user_id: str,
    target_author: str,
    target_timestamp: int,
    remove: bool = False,
) -> dict[str, str]:
    """React to a user's message with an emoji using signal-cli.

    ``target_author`` and ``target_timestamp`` identify the message being
    reacted to — use the ``sender_id`` and ``timestamp`` from receive_message.
    To react to your own "Note to Self" message, set ``user_id`` and
    ``target_author`` to your own number. Set ``remove=True`` to undo a reaction.
    """
    logger.info(f"Tool called: send_reaction_to_user for user {user_id}")
    _ensure_trusted(user_id)
    await _send_reaction(
        emoji,
        user_id,
        target_author,
        target_timestamp,
        is_group=False,
        remove=remove,
    )
    return {"message": "Reaction sent successfully"}


@mcp.tool()
@_log_tool_errors
async def send_reaction_to_group(
    emoji: str,
    group_id: str,
    target_author: str,
    target_timestamp: int,
    remove: bool = False,
) -> dict[str, str]:
    """React to a message in a group with an emoji using signal-cli.

    ``group_id`` is the group's internal id (the ``group_id`` returned by
    receive_message) or its display name. ``target_author`` and
    ``target_timestamp`` identify the message being reacted to. Set
    ``remove=True`` to undo a reaction.
    """
    logger.info(f"Tool called: send_reaction_to_group for group {group_id}")
    gid = await _ensure_trusted_group(group_id)
    await _send_reaction(
        emoji,
        gid,
        target_author,
        target_timestamp,
        is_group=True,
        remove=remove,
    )
    return {"message": "Reaction sent successfully"}


@mcp.tool()
@_log_tool_errors
async def send(message: str, attachments: list[str] | None = None) -> dict[str, str]:
    """Send a message to the channel operator's phone.

    Use this when the user asks to "send a message", "text me", or anything
    about sending to Signal. No phone number needed — it sends to the channel
    operator's number. ``attachments`` is an optional list of file paths (read
    on this server's host; embedded as data URIs when the daemon is remote),
    ``http(s)`` URLs (downloaded by the server), or RFC 2397 ``data:`` URIs;
    ``message`` may be empty when attachments are provided.
    """
    logger.info("Tool called: send")
    if not config.operator:
        raise SignalError("No operator configured (set --operator)")
    _ensure_trusted(config.operator)
    async with _prepared_attachments(attachments) as files:
        await _send_message(message, config.operator, is_group=False, attachments=files)
    return {"message": "Message sent successfully"}


@mcp.tool()
@_log_tool_errors
async def receive_message(timeout: float = 60.0) -> MessageResponse:
    """Wait for and receive a message using signal-cli.

    Returns the next actionable message (text body, attachments, or emoji
    reaction) that arrives within ``timeout`` seconds (default 60), or an
    empty result on timeout. Messages that arrived while the daemon was
    streaming to this server are queued, so back-to-back calls won't drop
    anything.

    File attachments are listed in ``attachments``: each entry carries the
    signal-cli attachment ``id``, ``content_type``, the sender's original
    ``filename`` (may be null), ``size`` in bytes, ``path`` — the absolute
    local path to the downloaded file, or null when the file is not present
    in the configured attachments directory — and ``url``, a short-lived
    presigned GET URL when S3 storage is enabled (else null; download it and
    read the file). An attachment-only message (e.g. a bare image) has
    ``message`` null but ``attachments`` populated.

    When a trusted-senders allowlist is configured, messages from other
    authors are skipped (with a log line) and the call keeps waiting within
    the timeout; with no allowlist configured, polling is unfiltered.

    Not available in channel mode: the background forwarder is the single
    consumer of the daemon's receive queue, so a concurrent ``receive_message``
    would race it and silently steal messages. In channel mode this raises
    instead — messages are pushed to you as ``notifications/claude/channel``.
    """
    logger.info(f"Tool called: receive_message with timeout {timeout}s")
    if config.channel_mode:
        raise SignalError(
            "receive_message is disabled in channel mode: the channel forwarder "
            "is the single consumer of inbound messages, which are delivered to "
            "you automatically as notifications/claude/channel."
        )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        try:
            result = (
                await get_client().next_message(remaining) if remaining > 0 else None
            )
        except SignalDisconnectedError:
            # A drop mid-wait is not an error for a poller — surface it as an
            # empty result (same as a timeout); the next call reconnects.
            logger.info("receive_message: daemon connection dropped mid-wait")
            return MessageResponse()
        if result is None:
            logger.info("No message received within timeout")
            return MessageResponse()
        if not is_trusted_sender(result.sender_id):
            logger.info(f"Dropped message from untrusted sender: {result.sender_id}")
            continue
        logger.info(
            f"Received message from {result.sender_id}"
            + (f" in group {result.group_id}" if result.group_id else "")
        )
        # Upload attachments to S3 (best-effort) so the result carries presigned
        # URLs; a failure leaves url=None and the local path is used instead.
        await s3.store_inbound_attachments(result)
        return result


@mcp.tool()
@_log_tool_errors
async def mark_read(sender: str, target_timestamp: int) -> dict[str, str]:
    """Mark a received message as read.

    Use this after receiving a message with ``receive_message`` to send a read
    receipt back to Signal. Pass the ``sender_id`` and ``timestamp`` from the
    received message.

    - ``sender`` *(str)* — the sender's phone number (E.164), from ``sender_id``.
    - ``target_timestamp`` *(int)* — the message timestamp, from ``timestamp``.
    """
    logger.info(f"Tool called: mark_read from {sender} ts={target_timestamp}")
    if not sender or not target_timestamp:
        raise ValueError("Both sender and target_timestamp are required")
    await _send_receipt(sender, target_timestamp)
    return {"message": "Read receipt sent"}
