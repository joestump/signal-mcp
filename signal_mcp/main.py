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
from typing import Optional, Dict, Union, Any
import asyncio
import json
import os
import argparse
from dataclasses import dataclass
import logging

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


SuccessResponse = Dict[str, str]
ErrorResponse = Dict[str, str]

# Global config instance
config = SignalConfig()


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
        success = await _send_message(message, user_id, is_group=False)
        if success:
            logger.info(f"Successfully sent message to user {user_id}")
            return {"message": "Message sent successfully"}
        logger.error(f"Failed to send message to user {user_id}")
        return {"error": "Failed to send message"}
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
    except Exception as e:
        logger.error(f"Error in send_reaction_to_group: {str(e)}", exc_info=True)
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


def initialize_server() -> SignalConfig:
    """Initialize the Signal server with configuration."""
    logger.info("Initializing Signal server")

    parser = argparse.ArgumentParser(description="Run the Signal MCP server")
    parser.add_argument(
        "--user-id", required=True, help="Signal phone number for the user"
    )
    parser.add_argument(
        "--transport",
        choices=["sse", "stdio"],
        default="sse",
        help="Transport to use for communication with the client. (default: sse)",
    )
    parser.add_argument(
        "--rpc-host",
        default=os.environ.get("SIGNAL_CLI_RPC_HOST", "127.0.0.1"),
        help="Host of the signal-cli daemon JSON-RPC interface "
        "(default: 127.0.0.1, env: SIGNAL_CLI_RPC_HOST)",
    )
    parser.add_argument(
        "--rpc-port",
        type=int,
        default=int(os.environ.get("SIGNAL_CLI_RPC_PORT", "7583")),
        help="Port of the signal-cli daemon JSON-RPC interface "
        "(default: 7583, env: SIGNAL_CLI_RPC_PORT)",
    )

    args = parser.parse_args()
    logger.info(
        f"Parsed arguments: user_id={args.user_id}, transport={args.transport}, "
        f"rpc={args.rpc_host}:{args.rpc_port}"
    )

    # Set global config
    config.user_id = args.user_id
    config.transport = args.transport
    config.rpc_host = args.rpc_host
    config.rpc_port = args.rpc_port

    logger.info(
        f"Initialized Signal server for user {config.user_id} "
        f"(daemon {config.rpc_host}:{config.rpc_port})"
    )
    return config


def run_mcp_server():
    """Run the MCP server in the current event loop."""
    config = initialize_server()

    transport = config.transport
    logger.info(f"Starting MCP server with transport: {transport}")

    return transport


def main():
    """Main function to run the Signal MCP server."""
    logger.info("Starting Signal MCP server")
    try:
        transport = run_mcp_server()
        mcp.run(transport)
    except Exception as e:
        logger.error(f"Error running Signal MCP server: {str(e)}", exc_info=True)
        raise
    finally:
        logger.info("Signal MCP server shutting down")


if __name__ == "__main__":
    main()
