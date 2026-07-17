"""Configuration for the Signal MCP server: CLI flags, env vars, and logging."""

import argparse
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Default directory scanned for user-defined prompt template files (*.md).
DEFAULT_PROMPTS_DIR = "~/.config/signal-mcp/prompts"


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
    # Directory of user-defined prompt templates (*.md files with YAML
    # frontmatter). A missing directory simply means no user prompts.
    prompts_dir: Path = field(
        default_factory=lambda: Path(DEFAULT_PROMPTS_DIR).expanduser()
    )
    log_level: str = "INFO"


# Global config instance shared by all modules.
config = SignalConfig()


def _normalize_recipient(value: str) -> str:
    """Normalize a recipient identifier for allowlist comparison."""
    return value.strip()


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


def configure_logging(level: str) -> None:
    """Configure root logging at the given level name (e.g. ``"INFO"``)."""
    logging.basicConfig(level=getattr(logging, level.upper()), format=LOG_FORMAT)


def parse_args(argv: list[str] | None = None) -> SignalConfig:
    """Parse CLI arguments and environment variables into the global config."""
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
        "The prefix must end on a word boundary and is stripped before "
        "delivery. (env: SIGNAL_MCP_PREFIX)",
    )
    parser.add_argument(
        "--prompts-dir",
        default=os.environ.get("SIGNAL_MCP_PROMPTS_DIR", DEFAULT_PROMPTS_DIR),
        help="Directory of user-defined prompt template files (*.md with YAML "
        "frontmatter). A missing directory just means no user prompts. "
        f"(default: {DEFAULT_PROMPTS_DIR}, env: SIGNAL_MCP_PROMPTS_DIR)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("SIGNAL_MCP_LOG_LEVEL", "INFO"),
        help="Logging verbosity: DEBUG, INFO, WARNING, ERROR, or CRITICAL. "
        "(default: INFO, env: SIGNAL_MCP_LOG_LEVEL)",
    )

    args = parser.parse_args(argv)

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
    log_level = args.log_level.upper()
    if log_level not in LOG_LEVELS:
        parser.error(
            f"invalid log level {args.log_level!r} "
            f"(choose one of {', '.join(LOG_LEVELS)})"
        )

    config.user_id = args.user_id
    config.transport = args.transport
    config.rpc_host = args.rpc_host
    config.rpc_port = args.rpc_port
    config.trusted_recipients = _load_trusted_recipients(args.trusted_recipients)
    config.channel_mode = args.channel
    config.prefix = args.prefix
    config.prompts_dir = Path(args.prompts_dir).expanduser()
    config.log_level = log_level

    # Channel mode always talks to Claude over stdio.
    if config.channel_mode:
        config.transport = "stdio"

    return config
