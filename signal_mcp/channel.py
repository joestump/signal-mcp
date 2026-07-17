"""Claude Channel mode: real-time push of Signal messages to Claude.

Instead of requiring Claude to poll ``receive_message``, channel mode runs a
background forwarder that reads messages from the signal-cli daemon and pushes
them as ``notifications/claude/channel`` JSON-RPC notifications over stdio.
"""

import asyncio
import contextlib
import logging
import re
from typing import Any

from mcp.server.stdio import stdio_server
from mcp.types import JSONRPCMessage, JSONRPCNotification

from signal_mcp.config import config, is_trusted_sender
from signal_mcp.parse import Attachment
from signal_mcp.prompts import SIGNAL_FORMATTING_RULES
from signal_mcp.rpc import SignalCLIError, get_client
from signal_mcp.tools import _send_receipt, mcp

logger = logging.getLogger(__name__)

CHANNEL_INSTRUCTIONS = (
    """\
This is a Signal messaging channel. The user can message you from their phone
via Signal, and you can message them back.

Inbound messages from Signal arrive as <channel source="signal" sender="..." \
sender_name="..." group="...">. The body text is the content.
Messages may carry file attachments, delivered as annotation lines in the
body like [attachment: /path/to/file (image/jpeg, 245 KB)]. The path is a
local file — open it with your Read tool (images render natively). A line
noting "file not available locally" means only the metadata is known.
Use send_message_to_user with the sender attribute as user_id to reply.
Use send to proactively message the user's phone (no phone number needed).
Always reply to acknowledge inbound messages, even if briefly.

"""
    + SIGNAL_FORMATTING_RULES
)

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")

# Channel forwarder resilience knobs. The forwarder blocks in next_message for
# up to _RECEIVE_TIMEOUT (a daemon disconnect wakes it sooner via the queue
# sentinel). When the daemon is unreachable it retries with exponential backoff
# from _INITIAL_BACKOFF up to _MAX_BACKOFF, so the channel survives a daemon
# that is down at startup or restarts mid-session.
_RECEIVE_TIMEOUT = 3600.0
_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 30.0


def _format_size(size: int | None) -> str:
    """Format a byte count human-readably (e.g. ``245 KB``).

    Uses 1024-based units, trims trailing zeros (``1.5 KB``, not ``1.50 KB``),
    and returns ``"unknown size"`` when the size is not known.
    """
    if size is None:
        return "unknown size"
    value = float(size)
    index = 0
    while value >= 1024 and index < len(_SIZE_UNITS) - 1:
        value /= 1024
        index += 1
    formatted = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{formatted} {_SIZE_UNITS[index]}"


def _attachment_line(attachment: Attachment) -> str:
    """Render one attachment as a bracketed annotation line for the channel.

    When the file resolved on disk the line carries its local path::

        [attachment: /path/to/file (image/png, 245 KB)]

    When ``path`` is ``None`` (file missing locally) the line falls back to
    the sender's original filename or the attachment id, and says so::

        [attachment: photo.png (image/png, 245 KB) — file not available locally]
    """
    content_type = attachment.content_type or "unknown type"
    size = _format_size(attachment.size)
    if attachment.path:
        return f"[attachment: {attachment.path} ({content_type}, {size})]"
    name = attachment.filename or attachment.id or "unknown"
    return f"[attachment: {name} ({content_type}, {size}) — file not available locally]"


def _strip_prefix(text: str, prefix: str) -> str | None:
    """Strip ``prefix`` from ``text`` when it matches on a word boundary.

    Matching is case-insensitive and ignores leading whitespace. When the
    prefix ends in a word character it must not run straight into another word
    character — prefix ``cc`` matches ``cc deploy`` and ``cc: deploy`` but not
    ``ccdeploy``. Returns the remaining text (whitespace-stripped), or ``None``
    when the prefix doesn't match.
    """
    stripped = text.lstrip()
    pattern = re.escape(prefix)
    if prefix and (prefix[-1].isalnum() or prefix[-1] == "_"):
        pattern += r"(?!\w)"
    match = re.match(pattern, stripped, re.IGNORECASE)
    if match is None:
        return None
    return stripped[match.end() :].lstrip()


async def _forward_channel_messages(write_stream: Any) -> None:
    """Background task: forward signal-cli messages to Claude as channel events.

    Runs alongside the MCP server. Reads messages from the signal-cli daemon's
    receive queue and pushes them as ``notifications/claude/channel`` so Claude
    sees them in real time without needing to poll ``receive_message``.

    The forwarder survives daemon outages for the life of the server: if the
    daemon is unreachable at startup or drops mid-session, ``next_message``
    raises :class:`SignalCLIError` (or returns ``None`` on a disconnect
    sentinel) and the loop retries with capped exponential backoff instead of
    dying.
    """
    rpc_client = get_client()
    logger.info("Channel forwarder: listening for messages")
    backoff = _INITIAL_BACKOFF

    while True:
        try:
            msg = await rpc_client.next_message(timeout=_RECEIVE_TIMEOUT)
            # A successful receive cycle (message or idle timeout) means the
            # daemon is reachable again; reset the backoff.
            backoff = _INITIAL_BACKOFF
            if msg is None:
                continue

            # Trusted-sender gating runs FIRST — before attachment handling,
            # prefix filtering, and receipts. The check applies to the
            # message author (envelope source), never the group id.
            # Untrusted senders are dropped: no notification, no read
            # receipt.
            if not is_trusted_sender(msg.sender_id):
                logger.info(
                    "Channel forwarder: dropped message from "
                    f"untrusted sender {msg.sender_id}"
                )
                continue

            # Skip reactions and truly-empty messages. A message with text
            # OR attachments (including attachment-only messages) forwards.
            if msg.reaction is not None or (not msg.message and not msg.attachments):
                continue

            text = msg.message or ""

            # Prefix filtering — if configured, only forward messages whose
            # text starts with the prefix (on a word boundary). The prefix
            # is stripped before delivery. The prefix applies to the text
            # portion only: an attachment-only message has no text to
            # match, so it is dropped (fail closed).
            if config.prefix:
                if not msg.message:
                    continue
                remainder = _strip_prefix(text, config.prefix)
                if remainder is None:
                    continue
                text = remainder

            # Each attachment contributes one annotation line; for
            # attachment-only messages the content is just those lines.
            content_parts = [text] if text else []
            content_parts.extend(_attachment_line(a) for a in msg.attachments)
            content = "\n".join(content_parts)

            meta: dict[str, str] = {"sender": msg.sender_id or ""}
            if msg.sender_name:
                meta["sender_name"] = msg.sender_name
            if msg.group_id:
                meta["group"] = msg.group_id

            notification = JSONRPCMessage(
                root=JSONRPCNotification(
                    jsonrpc="2.0",
                    method="notifications/claude/channel",
                    params={"content": content, "meta": meta},
                )
            )
            await write_stream.send(notification)
            logger.info(f"Channel forwarder: forwarded message from {msg.sender_id}")

            # Auto-mark as read so Signal shows the message as delivered/read.
            if msg.sender_id and msg.timestamp:
                try:
                    await _send_receipt(msg.sender_id, msg.timestamp)
                except Exception:  # noqa: BLE001
                    logger.warning("Channel forwarder: failed to send read receipt")
        except asyncio.CancelledError:
            raise
        except SignalCLIError as e:
            # The daemon is unreachable (down at startup, or dropped and not
            # yet back). Back off and retry — do not kill the forwarder.
            logger.warning(
                f"Channel forwarder: signal-cli daemon unreachable ({e}); "
                f"retrying in {backoff:.0f}s"
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Channel forwarder error: {e}")
            await asyncio.sleep(1)


async def run_channel_async() -> None:
    """Run the MCP server in Claude Channel mode over stdio.

    Declares the ``claude/channel`` experimental capability and starts a
    background task that forwards inbound signal-cli messages to Claude as
    ``notifications/claude/channel`` events.
    """
    async with stdio_server() as (read_stream, write_stream):
        init_options = mcp._mcp_server.create_initialization_options(
            experimental_capabilities={"claude/channel": {}}
        )
        forwarder = asyncio.create_task(_forward_channel_messages(write_stream))
        try:
            await mcp._mcp_server.run(read_stream, write_stream, init_options)
        finally:
            forwarder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await forwarder
            # Cancel the daemon reader task and close the socket on shutdown.
            await get_client().close()
