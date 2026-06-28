#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "mcp",
# ]
# ///
from mcp.server.fastmcp import FastMCP
from typing import Optional, Tuple, Dict, Union, Any
import asyncio
import json
import subprocess
import shlex
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
    """Configuration for Signal CLI."""

    user_id: str = ""  # The user's Signal phone number
    transport: str = "sse"


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
    """Exception raised when signal-cli command fails."""

    pass


SuccessResponse = Dict[str, str]
ErrorResponse = Dict[str, str]

# Global config instance
config = SignalConfig()


async def _run_signal_cli(cmd: str) -> Tuple[str, str, int | None]:
    """Helper method to run a signal-cli command."""
    logger.debug(f"Executing signal-cli command: {cmd}")
    try:
        process = await asyncio.create_subprocess_shell(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        stdout, stderr = await process.communicate()
        stdout_str, stderr_str = stdout.decode(), stderr.decode()

        if process.returncode != 0:
            logger.warning(
                f"signal-cli command failed with return code {process.returncode}"
            )
            logger.warning(f"stderr: {stderr_str}")
        else:
            logger.debug("signal-cli command completed successfully")

        return stdout_str, stderr_str, process.returncode

    except Exception as e:
        logger.error(f"Error running signal-cli command: {str(e)}", exc_info=True)
        raise SignalCLIError(f"Failed to run signal-cli: {str(e)}")


async def _get_group_id(group_name: str) -> Optional[str]:
    """Get the group name for a given group name."""
    logger.info(f"Looking up group with name: {group_name}")

    list_cmd = f"signal-cli -u {shlex.quote(config.user_id)} listGroups"
    stdout, stderr, return_code = await _run_signal_cli(list_cmd)

    if return_code != 0:
        logger.error(f"Error listing groups: {stderr}")
        return None

    # Parse the output to find the group name
    for line in stdout.split("\n"):
        if "Name: " in line and group_name in line:
            logger.info(f"Found group: {group_name}")
            return group_name

    logger.error(f"Could not find group with name: {group_name}")
    return None


async def _send_message(message: str, target: str, is_group: bool = False) -> bool:
    """Send a message to either a user or group."""
    target_type = "group" if is_group else "user"
    logger.info(f"Sending message to {target_type}: {target}")

    flag = "-g" if is_group else ""
    cmd = f"signal-cli -u {shlex.quote(config.user_id)} send {flag} {shlex.quote(target)} -m {shlex.quote(message)}"

    try:
        _, stderr, return_code = await _run_signal_cli(cmd)

        if return_code == 0:
            logger.info(f"Successfully sent message to {target_type}: {target}")
            return True
        else:
            logger.error(f"Error sending message to {target_type}: {stderr}")
            return False
    except SignalCLIError as e:
        logger.error(f"Failed to send message to {target_type}: {str(e)}")
        return False


async def _send_reaction(
    emoji: str,
    target: str,
    target_author: str,
    target_timestamp: int,
    is_group: bool = False,
    remove: bool = False,
) -> bool:
    """Send (or remove) an emoji reaction to a message.

    ``target`` is the recipient (a user number or a group id). ``target_author``
    and ``target_timestamp`` identify the message being reacted to.
    """
    target_type = "group" if is_group else "user"
    action = "Removing" if remove else "Sending"
    logger.info(f"{action} reaction {emoji!r} to {target_type}: {target}")

    recipient = "-g" if is_group else ""
    remove_flag = "-r" if remove else ""
    cmd = (
        f"signal-cli -u {shlex.quote(config.user_id)} sendReaction "
        f"{recipient} {shlex.quote(target)} "
        f"-e {shlex.quote(emoji)} "
        f"-a {shlex.quote(target_author)} "
        f"-t {int(target_timestamp)} "
        f"{remove_flag}"
    )

    try:
        _, stderr, return_code = await _run_signal_cli(cmd)

        if return_code == 0:
            logger.info(f"Successfully sent reaction to {target_type}: {target}")
            return True
        else:
            logger.error(f"Error sending reaction to {target_type}: {stderr}")
            return False
    except SignalCLIError as e:
        logger.error(f"Failed to send reaction to {target_type}: {str(e)}")
        return False


async def _parse_receive_output(
    stdout: str,
) -> Optional[MessageResponse]:
    """Parse signal-cli ``--output=json`` receive output.

    signal-cli emits one JSON envelope per line (JSON Lines). We return the
    first envelope that carries something meaningful — a text body or an emoji
    reaction — and skip the rest (delivery/read receipts, typing indicators and
    empty sync messages).

    JSON is required because signal-cli's plain-text output collapses a synced
    reaction (e.g. reacting to a "Note to Self" message) down to a bare
    ``Received a sync message`` line with no emoji or target, so reactions are
    impossible to recover from the text format.
    """
    logger.debug("Parsing received message output")

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            # signal-cli writes logs to stderr, but guard against stray lines.
            logger.debug(f"Skipping non-JSON line: {line[:80]!r}")
            continue

        envelope = payload.get("envelope") or {}
        sender = envelope.get("source") or envelope.get("sourceNumber")

        # A body/reaction lives on dataMessage (messages from others) or on
        # syncMessage.sentMessage (anything you sent from another linked device,
        # including reactions in "Note to Self").
        data_message = envelope.get("dataMessage") or {}
        sent_message = (envelope.get("syncMessage") or {}).get("sentMessage") or {}
        content: Dict[str, Any] = data_message or sent_message
        if not content:
            continue

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
                f"Parsed message from {sender}"
                + (f" in group {group}" if group else "")
            )
            return MessageResponse(
                message=body,
                sender_id=sender,
                group_name=group,
                timestamp=timestamp,
            )

    logger.warning("No parseable message or reaction found in output")
    return None


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
    """Send a message to a group using signal-cli."""
    logger.info(f"Tool called: send_message_to_group for group {group_id}")

    try:
        group_name = await _get_group_id(group_id)
        if not group_name:
            logger.error(f"Could not find group: {group_id}")
            return {"error": f"Could not find group: {group_id}"}

        success = await _send_message(message, group_name, is_group=True)
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
    receive_message). ``target_author`` and ``target_timestamp`` identify the
    message being reacted to. Set ``remove=True`` to undo a reaction.
    """
    logger.info(f"Tool called: send_reaction_to_group for group {group_id}")

    try:
        success = await _send_reaction(
            emoji,
            group_id,
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
    """Wait for and receive a message using signal-cli."""
    logger.info(f"Tool called: receive_message with timeout {timeout}s")

    try:
        cmd = f"signal-cli --output=json -u {shlex.quote(config.user_id)} receive --timeout {int(timeout)}"

        stdout, stderr, return_code = await _run_signal_cli(cmd)

        if return_code != 0:
            if "timeout" in stderr.lower():
                logger.info("Receive timeout reached with no messages")
                return MessageResponse()
            else:
                logger.error(f"Error receiving message: {stderr}")
                return MessageResponse(error=f"Failed to receive message: {stderr}")

        if not stdout.strip():
            logger.info("No message received within timeout")
            return MessageResponse()

        result = await _parse_receive_output(stdout)
        if result:
            logger.info(
                f"Successfully received message from {result.sender_id}"
                + (f" in group {result.group_name}" if result.group_name else "")
            )
            return result
        else:
            # Envelopes arrived but none carried a body or reaction (e.g. only
            # delivery/read receipts or typing indicators) — not an error.
            logger.info("Received envelopes with no message or reaction")
            return MessageResponse()

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

    args = parser.parse_args()
    logger.info(f"Parsed arguments: user_id={args.user_id}, transport={args.transport}")

    # Set global config
    config.user_id = args.user_id
    config.transport = args.transport

    logger.info(f"Initialized Signal server for user: {config.user_id}")
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
