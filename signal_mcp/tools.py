"""FastMCP tool definitions and send helpers for the Signal MCP server."""

import asyncio
import base64
import functools
import ipaddress
import logging
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ParamSpec, TypeVar
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

from signal_mcp.config import _normalize_recipient, config, is_trusted_sender
from signal_mcp.parse import MessageResponse
from signal_mcp.prompts import register_prompts
from signal_mcp.rpc import (
    SignalCLIError,
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


def _validate_attachments(attachments: list[str] | None) -> list[str] | None:
    """Validate outbound attachments before any RPC is issued.

    Entries starting with ``data:`` (RFC 2397 data URIs) pass through
    unchanged. Anything else must be an existing, readable file — ``~`` is
    expanded and the path is resolved to an absolute path. Raises
    :class:`SignalError` with a clear message on the first invalid entry, so
    the tool errors out before any daemon ``send`` is attempted.
    """
    if not attachments:
        return None

    validated: list[str] = []
    for entry in attachments:
        if entry.startswith("data:"):
            validated.append(entry)
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


def _encode_data_uri(path: Path) -> str:
    """Encode a local file as an RFC 2397 data URI for the daemon.

    Produces ``data:<mime>;filename=<basename>;base64,<data>`` — the MIME type
    is guessed from the file extension (``application/octet-stream`` when
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
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(data).decode("ascii")
    filename = quote(path.name, safe="")
    return f"data:{mime};filename={filename};base64,{encoded}"


def _prepare_attachments(attachments: list[str] | None) -> list[str] | None:
    """Validate outbound attachments and apply the configured transfer mode.

    Runs :func:`_validate_attachments` first, then — when the transfer mode
    resolves to ``data-uri`` — encodes each local file as an RFC 2397 data URI
    (see :func:`_encode_data_uri`). Caller-supplied ``data:`` URIs pass
    through unchanged in every mode. Raises :class:`SignalError` before any
    RPC is issued when validation fails or a file exceeds the size cap.
    Future transfer steps (e.g. URL downloads, #22) plug in here.
    """
    validated = _validate_attachments(attachments)
    if not validated or _resolve_transfer_mode() != "data-uri":
        return validated

    return [
        entry if entry.startswith("data:") else _encode_data_uri(Path(entry))
        for entry in validated
    ]


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

    ``attachments`` is an optional list of file paths or RFC 2397 ``data:``
    URIs (``data:<MIME>;filename=<NAME>;base64,<DATA>``). File paths are read
    on this server's host; when the daemon is remote they are embedded as
    data URIs automatically. ``message`` may be empty when attachments are
    provided.
    """
    logger.info(f"Tool called: send_message_to_user for user {user_id}")
    _ensure_trusted(user_id)
    files = _prepare_attachments(attachments)
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
    daemon is remote) or RFC 2397 ``data:`` URIs; ``message`` may be empty
    when attachments are provided.
    """
    logger.info(f"Tool called: send_message_to_group for group {group_id}")
    gid = await _ensure_trusted_group(group_id)
    files = _prepare_attachments(attachments)
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
    """Send a message to the channel owner's phone.

    Use this when the user asks to "send a message", "text me", or anything
    about sending to Signal. No phone number needed — it sends to the channel
    owner's number. ``attachments`` is an optional list of file paths (read
    on this server's host; embedded as data URIs when the daemon is remote)
    or RFC 2397 ``data:`` URIs; ``message`` may be empty when attachments are
    provided.
    """
    logger.info("Tool called: send")
    if not config.user_id:
        raise SignalError("No user_id configured (set --user-id)")
    _ensure_trusted(config.user_id)
    files = _prepare_attachments(attachments)
    await _send_message(message, config.user_id, is_group=False, attachments=files)
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
    ``filename`` (may be null), ``size`` in bytes, and ``path`` — the absolute
    local path to the downloaded file, or null when the file is not present
    in the configured attachments directory. An attachment-only message (e.g.
    a bare image) has ``message`` null but ``attachments`` populated.

    When a trusted-senders allowlist is configured, messages from other
    authors are skipped (with a log line) and the call keeps waiting within
    the timeout; with no allowlist configured, polling is unfiltered.
    """
    logger.info(f"Tool called: receive_message with timeout {timeout}s")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        result = await get_client().next_message(remaining) if remaining > 0 else None
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
