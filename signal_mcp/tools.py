"""FastMCP tool definitions and send helpers for the Signal MCP server."""

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

from mcp.server.fastmcp import FastMCP

from signal_mcp.config import _normalize_recipient, config
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


async def _send_message(message: str, target: str, is_group: bool = False) -> None:
    """Send a message to either a user or group via the daemon.

    Raises :class:`SignalCLIError` on failure.
    """
    target_type = "group" if is_group else "user"
    params: dict[str, Any] = {"message": message}
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
async def send_message_to_user(message: str, user_id: str) -> dict[str, str]:
    """Send a message to a specific user using signal-cli."""
    logger.info(f"Tool called: send_message_to_user for user {user_id}")
    _ensure_trusted(user_id)
    await _send_message(message, user_id, is_group=False)
    return {"message": "Message sent successfully"}


@mcp.tool()
@_log_tool_errors
async def send_message_to_group(message: str, group_id: str) -> dict[str, str]:
    """Send a message to a group using signal-cli.

    ``group_id`` may be the group's internal id (the ``group_id`` returned by
    receive_message) or its display name.
    """
    logger.info(f"Tool called: send_message_to_group for group {group_id}")
    gid = await _ensure_trusted_group(group_id)
    await _send_message(message, gid, is_group=True)
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
async def send(message: str) -> dict[str, str]:
    """Send a message to the channel owner's phone.

    Use this when the user asks to "send a message", "text me", or anything
    about sending to Signal. No phone number needed — it sends to the channel
    owner's number.
    """
    logger.info("Tool called: send")
    if not config.user_id:
        raise SignalError("No user_id configured (set --user-id)")
    _ensure_trusted(config.user_id)
    await _send_message(message, config.user_id, is_group=False)
    return {"message": "Message sent successfully"}


@mcp.tool()
@_log_tool_errors
async def receive_message(timeout: float = 60.0) -> MessageResponse:
    """Wait for and receive a message using signal-cli.

    Returns the next actionable message (text body or emoji reaction) that
    arrives within ``timeout`` seconds (default 60), or an empty result on
    timeout. Messages that arrived while the daemon was streaming to this
    server are queued, so back-to-back calls won't drop anything.
    """
    logger.info(f"Tool called: receive_message with timeout {timeout}s")
    result = await get_client().next_message(timeout)
    if result is None:
        logger.info("No message received within timeout")
        return MessageResponse()
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
