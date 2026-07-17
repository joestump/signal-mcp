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

from signal_mcp.config import config
from signal_mcp.rpc import SignalCLIError, get_client
from signal_mcp.tools import _send_receipt, mcp

logger = logging.getLogger(__name__)

CHANNEL_INSTRUCTIONS = """\
This is a Signal messaging channel. The user can message you from their phone
via Signal, and you can message them back.

Inbound messages from Signal arrive as <channel source="signal" sender="..." \
sender_name="..." group="...">. The body text is the content.
Use send_message_to_user with the sender attribute as user_id to reply.
Use send to proactively message the user's phone (no phone number needed).
Always reply to acknowledge inbound messages, even if briefly.
"""


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
    """
    rpc_client = get_client()
    try:
        await rpc_client.connect()
    except SignalCLIError as e:
        logger.error(f"Channel forwarder: cannot connect to signal-cli: {e}")
        return

    logger.info("Channel forwarder: listening for messages")

    while True:
        try:
            msg = await rpc_client.next_message(timeout=3600)
            if msg is None:
                continue
            # Only forward text messages, not reactions.
            if not msg.message:
                continue

            text = msg.message

            # Prefix filtering — if configured, only forward messages that
            # start with the prefix (on a word boundary). The prefix is
            # stripped before delivery.
            if config.prefix:
                remainder = _strip_prefix(text, config.prefix)
                if remainder is None:
                    continue
                text = remainder

            meta: dict[str, str] = {"sender": msg.sender_id or ""}
            if msg.sender_name:
                meta["sender_name"] = msg.sender_name
            if msg.group_id:
                meta["group"] = msg.group_id

            notification = JSONRPCMessage(
                root=JSONRPCNotification(
                    jsonrpc="2.0",
                    method="notifications/claude/channel",
                    params={"content": text, "meta": meta},
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
