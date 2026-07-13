#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "mcp",
# ]
# ///
"""Signal MCP server — JSON-RPC client for a long-running ``signal-cli daemon``.

Instead of spawning a fresh ``signal-cli`` (and a fresh JVM) per request, this
server talks to a persistent ``signal-cli -a <number> daemon --tcp HOST:PORT``
over its newline-delimited JSON-RPC interface. That daemon holds the account
lock for its lifetime, so:

  * calls are instant (no ~2-3s JVM cold start each time), and
  * concurrent callers (this MCP, scheduled tasks, manual use) no longer fight
    over the signal-cli account lock — everything funnels through one daemon.

The daemon is expected to run with ``--receive-mode on-start`` so it is always
receiving. Incoming messages arrive as JSON-RPC *notifications* (``method":
"receive"``); we drain the meaningful ones (text bodies / emoji reactions) into
a queue that ``receive_message`` pops from. signal-cli here is a *linked*
device, so the phone remains the durable source of truth — a brief daemon
outage never loses data.
"""

from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server
from mcp.types import JSONRPCMessage, JSONRPCNotification
from typing import Optional, Dict, Union, Any
import asyncio
import json
import os
import argparse
from dataclasses import dataclass, field
import logging
import anyio

# Set up logging with more detailed format
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Initialize MCP server
mcp = FastMCP(name="signal-cli")
logger.info("Initialized FastMCP server for signal-cli")


@dataclass
class SignalConfig:
    """Configuration for the Signal MCP server."""

    user_id: str = ""  # The user's Signal phone number (informational/logging)
    transport: str = "sse"
    rpc_host: str = "127.0.0.1"
    rpc_port: int = 7583
    # Allowlist of recipients (user phone numbers and/or group ids/names) the
    # server is permitted to message. When empty, enforcement is disabled and
    # every recipient is allowed (opt-in security).
    trusted_recipients: frozenset[str] = field(default_factory=frozenset)
    channel_mode: bool = False
    prefix: str = ""


@dataclass
class Reaction:
    """An emoji reaction to a message."""

    emoji: Optional[str] = None
    target_author: Optional[str] = None
    target_timestamp: Optional[int] = None
    is_remove: bool = False


@dataclass
class MessageResponse:
    """Structured result for received messages and reactions.

    A plain text message populates ``message``. An emoji reaction populates
    ``reaction`` instead (``message`` stays ``None``), so callers can tell the
    two apart.
    """

    message: Optional[str] = None
    sender_id: Optional[str] = None
    group_name: Optional[str] = None
    # Timestamp of the received message — pass it back as ``target_timestamp``
    # to react to this message.
    timestamp: Optional[int] = None
    error: Optional[str] = None
    reaction: Optional["Reaction"] = None


class SignalError(Exception):
    """Base exception for Signal-related errors."""

    pass


class SignalCLIError(SignalError):
    """Exception raised when a signal-cli JSON-RPC call fails."""

    pass


class UntrustedRecipientError(SignalError):
    """Raised when a send is attempted to a recipient not on the allowlist."""

    pass


SuccessResponse = Dict[str, str]
ErrorResponse = Dict[str, str]

# Global config instance
config = SignalConfig()


def _normalize_recipient(value: str) -> str:
    """Normalize a recipient identifier for allowlist comparison."""
    return value.strip()


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


def _envelope_to_response(payload: Dict[str, Any]) -> Optional[MessageResponse]:
    """Turn one signal-cli envelope into a ``MessageResponse``.

    ``payload`` is a single ``{"envelope": {...}, "account": ...}`` object — the
    ``params`` of a JSON-RPC ``receive`` notification (identical to one line of
    ``signal-cli --output=json receive``). Returns ``None`` for envelopes that
    carry nothing actionable (delivery/read receipts, typing indicators, empty
    sync messages).

    JSON is required because signal-cli's plain-text output collapses a synced
    reaction (e.g. reacting to a "Note to Self" message) down to a bare
    ``Received a sync message`` line with no emoji or target, so reactions are
    impossible to recover from the text format.
    """
    envelope = payload.get("envelope") or {}
    sender = envelope.get("source") or envelope.get("sourceNumber")

    # A body/reaction lives on dataMessage (messages from others) or on
    # syncMessage.sentMessage (anything you sent from another linked device,
    # including reactions in "Note to Self").
    data_message = envelope.get("dataMessage") or {}
    sent_message = (envelope.get("syncMessage") or {}).get("sentMessage") or {}
    content: Dict[str, Any] = data_message or sent_message
    if not content:
        return None

    group_info = content.get("groupInfo") or {}
    group = group_info.get("groupId")
    timestamp = content.get("timestamp") or envelope.get("timestamp")

    reaction = content.get("reaction")
    if reaction:
        emoji = reaction.get("emoji")
        logger.info(
            f"Parsed reaction {emoji!r} from {sender}"
            + (f" in group {group}" if group else "")
        )
        return MessageResponse(
            sender_id=sender,
            group_name=group,
            timestamp=timestamp,
            reaction=Reaction(
                emoji=emoji,
                target_author=reaction.get("targetAuthor"),
                target_timestamp=reaction.get("targetSentTimestamp"),
                is_remove=reaction.get("isRemove", False),
            ),
        )

    body = content.get("message")
    if body:
        logger.info(
            f"Parsed message from {sender}" + (f" in group {group}" if group else "")
        )
        return MessageResponse(
            message=body,
            sender_id=sender,
            group_name=group,
            timestamp=timestamp,
        )

    return None


class SignalRpcClient:
    """A persistent JSON-RPC client for a ``signal-cli daemon`` (TCP).

    One connection is opened lazily and kept alive. A background reader task
    routes responses back to their callers by ``id`` and funnels ``receive``
    notifications into a queue for :meth:`next_message`. If the connection drops
    it is transparently re-established on the next call.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._messages: asyncio.Queue = asyncio.Queue()
        self._id = 0
        self._connect_lock = asyncio.Lock()

    @property
    def _connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _ensure_connected(self) -> None:
        if self._connected:
            return
        async with self._connect_lock:
            if self._connected:
                return
            logger.info(f"Connecting to signal-cli daemon at {self.host}:{self.port}")
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    self.host, self.port
                )
            except OSError as e:
                raise SignalCLIError(
                    f"Cannot reach signal-cli daemon at {self.host}:{self.port} "
                    f"({e}). Is the daemon running? "
                    f"(macOS: `signal-daemon status`)"
                )
            self._reader_task = asyncio.create_task(self._read_loop())
            logger.info("Connected to signal-cli daemon")

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break  # EOF — daemon closed the connection
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(f"Skipping non-JSON line: {line[:80]!r}")
                    continue

                if "method" in obj:
                    # A notification. We only care about incoming messages.
                    if obj.get("method") == "receive":
                        parsed = _envelope_to_response(obj.get("params") or {})
                        if parsed is not None:
                            self._messages.put_nowait(parsed)
                    continue

                # Otherwise it's a response to one of our requests.
                rid = obj.get("id")
                fut = self._pending.pop(rid, None)
                if fut is None or fut.done():
                    continue
                if obj.get("error") is not None:
                    fut.set_exception(SignalCLIError(json.dumps(obj["error"])))
                else:
                    fut.set_result(obj.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — surface, then tear down cleanly
            logger.warning(f"signal-cli daemon reader loop ended: {e}")
        finally:
            self._teardown(SignalCLIError("signal-cli daemon connection closed"))

    def _teardown(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._writer = None
        self._reader = None

    async def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Any:
        """Issue a JSON-RPC request and await its result."""
        await self._ensure_connected()
        assert self._writer is not None

        self._id += 1
        rid = self._id
        req: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params is not None:
            req["params"] = params

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut

        logger.debug(f"JSON-RPC -> {method} (id={rid})")
        self._writer.write((json.dumps(req) + "\n").encode())
        await self._writer.drain()

        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise SignalCLIError(f"signal-cli daemon timed out on {method}")

    async def next_message(self, timeout: float) -> Optional[MessageResponse]:
        """Wait up to ``timeout`` seconds for the next actionable message."""
        await self._ensure_connected()
        try:
            return await asyncio.wait_for(self._messages.get(), timeout)
        except asyncio.TimeoutError:
            return None


# Global JSON-RPC client (initialized in initialize_server()).
client: Optional[SignalRpcClient] = None


def _client() -> SignalRpcClient:
    global client
    if client is None:
        client = SignalRpcClient(config.rpc_host, config.rpc_port)
    return client


# --------------------------------------------------------------------------- #
# Claude Channel support
# --------------------------------------------------------------------------- #

CHANNEL_INSTRUCTIONS = """\
This is a Signal messaging channel. The user can message you from their phone
via Signal, and you can message them back.

Inbound messages from Signal arrive as <channel source="signal" sender="..." \
sender_name="..." group="...">. The body text is the content.
Use send_message_to_user with the sender attribute as user_id to reply.
Use send to proactively message the user's phone (no phone number needed).
Always reply to acknowledge inbound messages, even if briefly.
"""


async def _forward_channel_messages(
    write_stream: Any,
) -> None:
    """Background task: forward signal-cli messages to Claude as channel events.

    Runs alongside the MCP server. Reads messages from the signal-cli daemon's
    receive queue and pushes them as ``notifications/claude/channel`` so Claude
    sees them in real time without needing to poll ``receive_message``.
    """
    rpc = _client()
    try:
        await rpc._ensure_connected()
    except SignalCLIError as e:
        logger.error(f"Channel forwarder: cannot connect to signal-cli: {e}")
        return

    logger.info("Channel forwarder: listening for messages")

    while True:
        try:
            msg = await rpc.next_message(timeout=3600)
            if msg is None:
                continue
            # Only forward text messages, not reactions.
            if not msg.message:
                continue

            text = msg.message

            # Prefix filtering — if configured, only forward messages that
            # start with the prefix. The prefix is stripped before delivery.
            if config.prefix:
                stripped = text.lstrip()
                if not stripped.lower().startswith(config.prefix.lower()):
                    continue
                text = stripped[len(config.prefix) :].lstrip()

            meta: Dict[str, str] = {"sender": msg.sender_id or ""}
            if msg.group_name:
                meta["group"] = msg.group_name

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


async def _resolve_group_id(name_or_id: str) -> Optional[str]:
    """Resolve a group name *or* id to the group's internal id."""
    logger.info(f"Resolving group: {name_or_id}")
    try:
        groups = await _client().call("listGroups")
    except SignalCLIError as e:
        logger.error(f"Error listing groups: {e}")
        return None

    for g in groups or []:
        if g.get("id") == name_or_id or g.get("name") == name_or_id:
            logger.info(f"Resolved group {name_or_id!r} -> {g.get('id')}")
            return g.get("id")

    logger.error(f"Could not find group: {name_or_id}")
    return None


async def _send_receipt(
    sender: str,
    target_timestamp: int,
) -> bool:
    """Send a read receipt for a received message via the daemon.

    Read receipts are always addressed to the individual author of the message,
    even when it was posted in a group — signal-cli has no ``groupId`` param
    for ``sendReceipt``.
    """
    params: Dict[str, Any] = {
        "recipient": [sender],
        "targetTimestamp": int(target_timestamp),
        "type": "read",
    }

    try:
        await _client().call("sendReceipt", params)
        logger.info(f"Sent read receipt for message from {sender}")
        return True
    except SignalCLIError as e:
        logger.error(f"Failed to send read receipt: {e}")
        return False


async def _send_message(message: str, target: str, is_group: bool = False) -> bool:
    """Send a message to either a user or group via the daemon."""
    target_type = "group" if is_group else "user"
    logger.info(f"Sending message to {target_type}: {target}")

    params: Dict[str, Any] = {"message": message}
    if is_group:
        params["groupId"] = target
    else:
        params["recipient"] = [target]

    try:
        await _client().call("send", params)
        logger.info(f"Successfully sent message to {target_type}: {target}")
        return True
    except SignalCLIError as e:
        logger.error(f"Failed to send message to {target_type}: {e}")
        return False


async def _send_reaction(
    emoji: str,
    target: str,
    target_author: str,
    target_timestamp: int,
    is_group: bool = False,
    remove: bool = False,
) -> bool:
    """Send (or remove) an emoji reaction to a message via the daemon.

    ``target`` is the recipient (a user number or a group id). ``target_author``
    and ``target_timestamp`` identify the message being reacted to.
    """
    target_type = "group" if is_group else "user"
    action = "Removing" if remove else "Sending"
    logger.info(f"{action} reaction {emoji!r} to {target_type}: {target}")

    params: Dict[str, Any] = {
        "emoji": emoji,
        "targetAuthor": target_author,
        "targetTimestamp": int(target_timestamp),
        "remove": remove,
    }
    if is_group:
        params["groupId"] = target
    else:
        params["recipient"] = [target]

    try:
        await _client().call("sendReaction", params)
        logger.info(f"Successfully sent reaction to {target_type}: {target}")
        return True
    except SignalCLIError as e:
        logger.error(f"Failed to send reaction to {target_type}: {e}")
        return False


@mcp.tool()
async def send_message_to_user(
    message: str, user_id: str
) -> Union[SuccessResponse, ErrorResponse]:
    """Send a message to a specific user using signal-cli."""
    logger.info(f"Tool called: send_message_to_user for user {user_id}")

    try:
        _ensure_trusted(user_id)
        success = await _send_message(message, user_id, is_group=False)
        if success:
            logger.info(f"Successfully sent message to user {user_id}")
            return {"message": "Message sent successfully"}
        logger.error(f"Failed to send message to user {user_id}")
        return {"error": "Failed to send message"}
    except UntrustedRecipientError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Error in send_message_to_user: {str(e)}", exc_info=True)
        return {"error": str(e)}


@mcp.tool()
async def send_message_to_group(
    message: str, group_id: str
) -> Union[SuccessResponse, ErrorResponse]:
    """Send a message to a group using signal-cli.

    ``group_id`` may be the group's internal id (the ``group_name`` returned by
    receive_message) or its display name.
    """
    logger.info(f"Tool called: send_message_to_group for group {group_id}")

    try:
        _ensure_trusted(group_id)
        gid = await _resolve_group_id(group_id)
        if not gid:
            logger.error(f"Could not find group: {group_id}")
            return {"error": f"Could not find group: {group_id}"}

        success = await _send_message(message, gid, is_group=True)
        if success:
            logger.info(f"Successfully sent message to group {group_id}")
            return {"message": "Message sent successfully"}
        logger.error(f"Failed to send message to group {group_id}")
        return {"error": "Failed to send message"}
    except UntrustedRecipientError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Error in send_message_to_group: {str(e)}", exc_info=True)
        return {"error": str(e)}


@mcp.tool()
async def send_reaction_to_user(
    emoji: str,
    user_id: str,
    target_author: str,
    target_timestamp: int,
    remove: bool = False,
) -> Union[SuccessResponse, ErrorResponse]:
    """React to a user's message with an emoji using signal-cli.

    ``target_author`` and ``target_timestamp`` identify the message being
    reacted to — use the ``sender_id`` and ``timestamp`` from receive_message.
    To react to your own "Note to Self" message, set ``user_id`` and
    ``target_author`` to your own number. Set ``remove=True`` to undo a reaction.
    """
    logger.info(f"Tool called: send_reaction_to_user for user {user_id}")

    try:
        _ensure_trusted(user_id)
        success = await _send_reaction(
            emoji,
            user_id,
            target_author,
            target_timestamp,
            is_group=False,
            remove=remove,
        )
        if success:
            return {"message": "Reaction sent successfully"}
        return {"error": "Failed to send reaction"}
    except UntrustedRecipientError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Error in send_reaction_to_user: {str(e)}", exc_info=True)
        return {"error": str(e)}


@mcp.tool()
async def send_reaction_to_group(
    emoji: str,
    group_id: str,
    target_author: str,
    target_timestamp: int,
    remove: bool = False,
) -> Union[SuccessResponse, ErrorResponse]:
    """React to a message in a group with an emoji using signal-cli.

    ``group_id`` is the group's internal id (the ``group_name`` returned by
    receive_message) or its display name. ``target_author`` and
    ``target_timestamp`` identify the message being reacted to. Set
    ``remove=True`` to undo a reaction.
    """
    logger.info(f"Tool called: send_reaction_to_group for group {group_id}")

    try:
        _ensure_trusted(group_id)
        gid = await _resolve_group_id(group_id)
        if not gid:
            return {"error": f"Could not find group: {group_id}"}

        success = await _send_reaction(
            emoji,
            gid,
            target_author,
            target_timestamp,
            is_group=True,
            remove=remove,
        )
        if success:
            return {"message": "Reaction sent successfully"}
        return {"error": "Failed to send reaction"}
    except UntrustedRecipientError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Error in send_reaction_to_group: {str(e)}", exc_info=True)
        return {"error": str(e)}


@mcp.tool()
async def send(message: str) -> Union[SuccessResponse, ErrorResponse]:
    """Send a message to the channel owner's phone.

    Use this when the user asks to "send a message", "text me", or anything
    about sending to Signal. No phone number needed — it sends to the channel
    owner's number.
    """
    logger.info("Tool called: send")
    if not config.user_id:
        return {"error": "No user_id configured (set --user-id)"}
    try:
        _ensure_trusted(config.user_id)
        success = await _send_message(message, config.user_id, is_group=False)
        if success:
            return {"message": "Message sent successfully"}
        return {"error": "Failed to send message"}
    except UntrustedRecipientError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Error in send: {str(e)}", exc_info=True)
        return {"error": str(e)}


@mcp.tool()
async def receive_message(timeout: float) -> MessageResponse:
    """Wait for and receive a message using signal-cli.

    Returns the next actionable message (text body or emoji reaction) that
    arrives within ``timeout`` seconds, or an empty result on timeout. Messages
    that arrived while the daemon was streaming to this server are queued, so
    back-to-back calls won't drop anything.
    """
    logger.info(f"Tool called: receive_message with timeout {timeout}s")

    try:
        result = await _client().next_message(timeout)
        if result is None:
            logger.info("No message received within timeout")
            return MessageResponse()
        logger.info(
            f"Successfully received message from {result.sender_id}"
            + (f" in group {result.group_name}" if result.group_name else "")
        )
        return result
    except SignalCLIError as e:
        logger.error(f"Error receiving message: {e}")
        return MessageResponse(error=str(e))
    except Exception as e:
        logger.error(f"Error in receive_message: {str(e)}", exc_info=True)
        return MessageResponse(error=str(e))


@mcp.tool()
async def mark_read(
    sender: str,
    target_timestamp: int,
) -> Union[SuccessResponse, ErrorResponse]:
    """Mark a received message as read.

    Use this after receiving a message with ``receive_message`` to send a read
    receipt back to Signal. Pass the ``sender_id`` and ``timestamp`` from the
    received message.

    - ``sender`` *(str)* — the sender's phone number (E.164), from ``sender_id``.
    - ``target_timestamp`` *(int)* — the message timestamp, from ``timestamp``.
    """
    logger.info(f"Tool called: mark_read from {sender} ts={target_timestamp}")

    if not sender or not target_timestamp:
        return {"error": "Both sender and target_timestamp are required"}

    success = await _send_receipt(sender, target_timestamp)
    if success:
        return {"message": "Read receipt sent"}
    return {"error": "Failed to send read receipt"}


def initialize_server() -> SignalConfig:
    """Initialize the Signal server with configuration."""
    logger.info("Initializing Signal server")

    parser = argparse.ArgumentParser(description="Run the Signal MCP server")
    parser.add_argument(
        "--user-id",
        default=os.environ.get("SIGNAL_MCP_USER_ID"),
        help="Signal phone number for the user (env: SIGNAL_MCP_USER_ID)",
    )
    parser.add_argument(
        "--transport",
        choices=["sse", "stdio"],
        default=os.environ.get("SIGNAL_MCP_TRANSPORT", "sse"),
        help="Transport to use for communication with the client. "
        "(default: sse, env: SIGNAL_MCP_TRANSPORT)",
    )
    parser.add_argument(
        "--rpc-host",
        default=os.environ.get("SIGNAL_MCP_RPC_HOST", "127.0.0.1"),
        help="Host of the signal-cli daemon JSON-RPC interface "
        "(default: 127.0.0.1, env: SIGNAL_MCP_RPC_HOST)",
    )
    parser.add_argument(
        "--rpc-port",
        type=int,
        default=int(os.environ.get("SIGNAL_MCP_RPC_PORT", "7583")),
        help="Port of the signal-cli daemon JSON-RPC interface "
        "(default: 7583, env: SIGNAL_MCP_RPC_PORT)",
    )
    parser.add_argument(
        "--trusted-recipient",
        action="append",
        default=[],
        dest="trusted_recipients",
        metavar="RECIPIENT",
        help=(
            "Phone number or group id/name the server is allowed to message. "
            "Repeat the flag to allow several. Values from the "
            "SIGNAL_MCP_TRUSTED_RECIPIENTS env var (comma-separated) are added "
            "too. If no trusted recipients are configured, every recipient is "
            "permitted."
        ),
    )
    parser.add_argument(
        "--channel",
        action="store_true",
        default=os.environ.get("SIGNAL_MCP_CHANNEL", "").lower()
        in ("1", "true", "yes"),
        help="Enable Claude Channel mode — push messages to Claude via "
        "notifications/claude/channel instead of requiring polling. "
        "(env: SIGNAL_MCP_CHANNEL)",
    )
    parser.add_argument(
        "--prefix",
        default=os.environ.get("SIGNAL_MCP_PREFIX", ""),
        help="Only forward messages starting with this prefix (channel mode). "
        "The prefix is stripped before delivery. (env: SIGNAL_MCP_PREFIX)",
    )

    args = parser.parse_args()

    # --user-id is required, but may come from the environment instead of the
    # flag, so validate after parsing rather than with argparse's required=True.
    if not args.user_id:
        parser.error("--user-id is required (or set SIGNAL_MCP_USER_ID)")
    # choices isn't enforced for values coming from a default (i.e. the env var).
    if args.transport not in ("sse", "stdio"):
        parser.error(
            f"invalid transport {args.transport!r} "
            "(set SIGNAL_MCP_TRANSPORT to 'sse' or 'stdio')"
        )

    logger.info(
        f"Parsed arguments: user_id={args.user_id}, transport={args.transport}, "
        f"rpc={args.rpc_host}:{args.rpc_port}, channel={args.channel}, "
        f"prefix={args.prefix!r}"
    )

    # Set global config
    config.user_id = args.user_id
    config.transport = args.transport
    config.rpc_host = args.rpc_host
    config.rpc_port = args.rpc_port
    config.trusted_recipients = _load_trusted_recipients(args.trusted_recipients)
    config.channel_mode = args.channel
    config.prefix = args.prefix

    # In channel mode, set instructions and use stdio transport.
    if config.channel_mode:
        config.transport = "stdio"
        mcp._mcp_server.instructions = CHANNEL_INSTRUCTIONS
        logger.info("Claude Channel mode enabled")

    if config.trusted_recipients:
        logger.info(
            f"Trusted recipients enforced ({len(config.trusted_recipients)} "
            "entries); sends to other recipients will be rejected"
        )
    else:
        logger.warning(
            "No trusted recipients configured; the server may message any recipient"
        )

    logger.info(
        f"Initialized Signal server for user {config.user_id} "
        f"(daemon {config.rpc_host}:{config.rpc_port})"
    )
    return config


def _load_trusted_recipients(cli_recipients: list[str]) -> frozenset[str]:
    """Build the trusted-recipient allowlist from CLI flags and the environment.

    Combines ``--trusted-recipient`` flags with the comma-separated
    ``SIGNAL_MCP_TRUSTED_RECIPIENTS`` env var, normalizing and dropping blanks.
    """
    recipients = list(cli_recipients or [])
    env_value = os.environ.get("SIGNAL_MCP_TRUSTED_RECIPIENTS", "")
    recipients.extend(env_value.split(","))

    return frozenset(
        normalized for raw in recipients if (normalized := _normalize_recipient(raw))
    )


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
            try:
                await forwarder
            except asyncio.CancelledError:
                pass


def main():
    """Main function to run the Signal MCP server."""
    logger.info("Starting Signal MCP server")
    try:
        cfg = initialize_server()
        if cfg.channel_mode:
            anyio.run(run_channel_async)
        else:
            mcp.run(cfg.transport)
    except Exception as e:
        logger.error(f"Error running Signal MCP server: {str(e)}", exc_info=True)
        raise
    finally:
        logger.info("Signal MCP server shutting down")


if __name__ == "__main__":
    main()
