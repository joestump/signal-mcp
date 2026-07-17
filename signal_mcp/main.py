#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "mcp",
#     "anyio",
# ]
# ///
"""CLI entrypoint for the Signal MCP server.

The server is a JSON-RPC client for a long-running ``signal-cli daemon``.
Instead of spawning a fresh ``signal-cli`` (and a fresh JVM) per request, it
talks to a persistent ``signal-cli -a <number> daemon --tcp HOST:PORT`` over
its newline-delimited JSON-RPC interface. That daemon holds the account lock
for its lifetime, so:

  * calls are instant (no ~2-3s JVM cold start each time), and
  * concurrent callers (this MCP, scheduled tasks, manual use) no longer fight
    over the signal-cli account lock — everything funnels through one daemon.

The daemon is expected to run with ``--receive-mode on-start`` so it is always
receiving. Incoming messages arrive as JSON-RPC *notifications* (``method":
"receive"``); we drain the meaningful ones (text bodies / emoji reactions) into
a queue that ``receive_message`` pops from. signal-cli here is a *linked*
device, so the phone remains the durable source of truth — a brief daemon
outage never loses data.

The implementation lives in sibling modules — :mod:`signal_mcp.config`
(configuration), :mod:`signal_mcp.rpc` (daemon client), :mod:`signal_mcp.parse`
(envelope parsing), :mod:`signal_mcp.tools` (MCP tools), and
:mod:`signal_mcp.channel` (Claude Channel mode).
"""

import logging
import sys
from pathlib import Path
from typing import Literal, cast

if __package__ in (None, ""):
    # Support `python signal_mcp/main.py` without installing the package.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anyio

from signal_mcp.channel import (
    CHANNEL_INSTRUCTIONS,
    _forward_channel_messages,
    run_channel_async,
)
from signal_mcp.config import (
    SignalConfig,
    _load_trusted_recipients,
    _normalize_recipient,
    config,
    configure_logging,
    parse_args,
)
from signal_mcp.parse import MessageResponse, Reaction, _envelope_to_response
from signal_mcp.prompts import load_user_prompts
from signal_mcp.rpc import (
    SignalCLIError,
    SignalError,
    SignalRpcClient,
    UntrustedRecipientError,
    get_client,
)
from signal_mcp.tools import (
    mark_read,
    mcp,
    receive_message,
    send,
    send_message_to_group,
    send_message_to_user,
    send_reaction_to_group,
    send_reaction_to_user,
)

# Backwards-compatible re-exports: everything above used to live in this module
# before the split into config/parse/rpc/channel/tools.
__all__ = [
    "CHANNEL_INSTRUCTIONS",
    "MessageResponse",
    "Reaction",
    "SignalCLIError",
    "SignalConfig",
    "SignalError",
    "SignalRpcClient",
    "UntrustedRecipientError",
    "_envelope_to_response",
    "_forward_channel_messages",
    "_load_trusted_recipients",
    "_normalize_recipient",
    "config",
    "get_client",
    "main",
    "mark_read",
    "mcp",
    "receive_message",
    "run_channel_async",
    "send",
    "send_message_to_group",
    "send_message_to_user",
    "send_reaction_to_group",
    "send_reaction_to_user",
]

logger = logging.getLogger(__name__)


def _log_startup(cfg: SignalConfig) -> None:
    """Log the effective configuration once logging is set up."""
    logger.info(
        f"Starting Signal MCP server for user {cfg.user_id} "
        f"(transport={cfg.transport}, daemon {cfg.rpc_host}:{cfg.rpc_port}, "
        f"channel={cfg.channel_mode}, prefix={cfg.prefix!r})"
    )
    if cfg.trusted_recipients:
        logger.info(
            f"Trusted recipients enforced ({len(cfg.trusted_recipients)} "
            "entries); sends to other recipients will be rejected"
        )
    else:
        logger.warning(
            "No trusted recipients configured; the server may message any recipient"
        )


def main() -> None:
    """Parse configuration and run the Signal MCP server."""
    cfg = parse_args()
    configure_logging(cfg.log_level)
    _log_startup(cfg)
    user_prompts = load_user_prompts(mcp, cfg.prompts_dir)
    if user_prompts:
        logger.info(f"Loaded {user_prompts} user prompt(s) from {cfg.prompts_dir}")
    try:
        if cfg.channel_mode:
            mcp._mcp_server.instructions = CHANNEL_INSTRUCTIONS
            logger.info("Claude Channel mode enabled")
            anyio.run(run_channel_async)
        else:
            mcp.run(cast(Literal["sse", "stdio"], cfg.transport))
    except Exception as e:
        logger.error(f"Error running Signal MCP server: {e}", exc_info=True)
        raise
    finally:
        logger.info("Signal MCP server shutting down")


if __name__ == "__main__":
    main()
