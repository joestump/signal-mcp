"""Configuration for the Signal MCP server: CLI flags, env vars, and logging."""

import argparse
import ipaddress
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Default directory scanned for user-defined prompt template files (*.md).
DEFAULT_PROMPTS_DIR = "~/.config/signal-mcp/prompts"

# Where signal-cli (>= 0.14.6) stores received attachments on disk, keyed by
# the attachment id (which includes the file extension).
DEFAULT_ATTACHMENTS_DIR = "~/.local/share/signal-cli/attachments"

# Outbound attachment transfer (#18): how local file attachments are handed
# to the signal-cli daemon.
ATTACHMENT_TRANSFER_MODES = ("auto", "path", "data-uri")
DEFAULT_ATTACHMENT_MAX_BYTES = 26214400  # 25 MB


@dataclass
class SignalConfig:
    """Configuration for the Signal MCP server."""

    # The human this agent serves — the default recipient of the `send`
    # ("text me") tool and, in channel mode, the default trusted inbound
    # sender. This is NOT the number the MCP runs as.
    operator: str = ""
    # The Signal number the MCP runs as (the signal-cli daemon's `-a` account;
    # messages are sent FROM this number). Informational/logging today, and
    # the identity the daemon must be bound to. Defaults to `operator` when unset,
    # which is the single-number/Note-to-Self case (agent talks to itself).
    account: str = ""
    transport: str = "sse"
    rpc_host: str = "127.0.0.1"
    rpc_port: int = 7583
    # Allowlist of recipients (user phone numbers and/or group ids/names) the
    # server is permitted to message. When empty, enforcement is disabled and
    # every recipient is allowed (opt-in security).
    trusted_recipients: frozenset[str] = field(default_factory=frozenset)
    # Allowlist of message authors (envelope ``source``) whose inbound
    # messages may reach the agent. When empty, channel mode denies everyone
    # but ``operator`` (deny-by-default), while polling stays ungated.
    trusted_senders: frozenset[str] = field(default_factory=frozenset)
    channel_mode: bool = False
    prefix: str = ""
    # Directory of user-defined prompt templates (*.md files with YAML
    # frontmatter). A missing directory simply means no user prompts.
    prompts_dir: Path = field(
        default_factory=lambda: Path(DEFAULT_PROMPTS_DIR).expanduser()
    )
    log_level: str = "INFO"
    # S3-compatible attachment storage. Setting a bucket enables S3 mode.
    # Credentials come exclusively from the standard AWS chain (env vars,
    # shared config files, instance roles) — never from flags.
    s3_bucket: str = ""
    s3_endpoint_url: str = ""  # empty = AWS default endpoint
    s3_region: str = ""
    s3_prefix: str = "signal-mcp/"
    s3_presign_ttl: int = 3600
    s3_force_path_style: bool = False
    # Directory where signal-cli stores received attachment files.
    attachments_dir: str = field(
        default_factory=lambda: os.path.expanduser(DEFAULT_ATTACHMENTS_DIR)
    )
    # How outbound file attachments reach the daemon: "path" passes local
    # file paths (requires a shared filesystem with the daemon), "data-uri"
    # embeds the file content as an RFC 2397 data URI, and "auto" picks
    # data-uri when rpc_host is not a loopback address, path otherwise.
    attachment_transfer: str = "auto"
    # Largest local file (in bytes) that may be encoded as a data URI.
    attachment_max_bytes: int = DEFAULT_ATTACHMENT_MAX_BYTES
    # History buffer caps (A2UI chat surfaces, SPEC-0001).
    # Per-conversation message cap (FIFO eviction).
    history_message_cap: int = 200
    # Total conversation cap (LRU eviction of least-recently-active).
    history_conversation_cap: int = 50
    # Per-message stored-text cap in bytes (truncation with marker).
    history_text_cap: int = 4096
    # Reply routing (SPEC-0002). Active only in channel mode over HTTP.
    # default_agent receives unrouted traffic; empty means fan out to every
    # live session.
    default_agent: str = ""
    route_ttl: int = 604800
    route_max_entries: int = 10000
    # Central HTTP channel server (SPEC-0002). auth_token empty means auth is
    # off — permitted only with --allow-unauthenticated on a loopback bind.
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    auth_token: str = ""
    allow_unauthenticated: bool = False

    @property
    def routing_enabled(self) -> bool:
        """True when reply routing is active (channel mode over HTTP)."""
        return self.channel_mode and self.transport == "http"


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


def _env_tristate(name: str) -> bool | None:
    """Parse a boolean env var, returning ``None`` when unset or blank."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return None
    return raw in ("1", "true", "yes", "on")


def _load_trusted_senders(cli_senders: list[str]) -> frozenset[str]:
    """Build the trusted-sender allowlist from CLI flags and the environment.

    Combines ``--trusted-sender`` flags with the comma-separated
    ``SIGNAL_MCP_TRUSTED_SENDERS`` env var, normalizing and dropping blanks —
    the same rules as trusted recipients.
    """
    senders = list(cli_senders or [])
    env_value = os.environ.get("SIGNAL_MCP_TRUSTED_SENDERS", "")
    senders.extend(env_value.split(","))

    return frozenset(
        normalized for raw in senders if (normalized := _normalize_recipient(raw))
    )


def is_trusted_sender(sender: str | None) -> bool:
    """Decide whether an inbound message author may reach the agent.

    The check always applies to the message *author* (the envelope
    ``source``), never a group id — membership in a group must not grant
    prompt injection.

    - When trusted senders are configured, only allowlisted authors pass.
      The list is exhaustive: include your own number if you want your own
      messages (e.g. Note to Self) through.
    - When none are configured and channel mode is enabled, only the channel
      operator (``operator``) passes — inbound gating is deny-by-default in
      channel mode.
    - Otherwise (polling mode with no allowlist) every author passes, so
      plain polling behavior is unchanged.
    """
    normalized = _normalize_recipient(sender or "")
    if config.trusted_senders:
        return normalized in config.trusted_senders
    if config.channel_mode:
        return bool(normalized) and normalized == _normalize_recipient(config.operator)
    return True


def configure_logging(level: str) -> None:
    """Configure root logging at the given level name (e.g. ``"INFO"``)."""
    logging.basicConfig(level=getattr(logging, level.upper()), format=LOG_FORMAT)


def parse_args(argv: list[str] | None = None) -> SignalConfig:
    """Parse CLI arguments and environment variables into the global config."""
    parser = argparse.ArgumentParser(description="Run the Signal MCP server")
    parser.add_argument(
        "--operator",
        default=os.environ.get("SIGNAL_MCP_OPERATOR"),
        help="Signal phone number (E.164) of the human this agent serves — the "
        "default recipient of the `send` tool and, in channel mode, the "
        "default trusted inbound sender. This is who the agent talks TO, not "
        "the number it runs as. Required. (env: SIGNAL_MCP_OPERATOR)",
    )
    parser.add_argument(
        "--account",
        default=os.environ.get("SIGNAL_MCP_ACCOUNT"),
        help="Signal phone number (E.164) the MCP runs AS — the signal-cli "
        "daemon's account, which messages are sent FROM. Defaults to --operator "
        "(the single-number/Note-to-Self case). Set this when the agent has "
        "its own number distinct from the operator. (env: SIGNAL_MCP_ACCOUNT)",
    )
    parser.add_argument(
        "--transport",
        choices=["sse", "stdio", "http"],
        default=os.environ.get("SIGNAL_MCP_TRANSPORT", "sse"),
        help="Transport to use for communication with the client. 'http' is "
        "the streamable-HTTP transport and requires --channel (the central "
        "channel server). (default: sse, env: SIGNAL_MCP_TRANSPORT)",
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
        "--trusted-sender",
        action="append",
        default=[],
        dest="trusted_senders",
        metavar="SENDER",
        help=(
            "Phone number (envelope source) whose inbound messages may reach "
            "the agent. Repeat the flag to allow several. Values from the "
            "SIGNAL_MCP_TRUSTED_SENDERS env var (comma-separated) are added "
            "too. In channel mode, when no trusted senders are configured, "
            "only messages from --operator are forwarded (deny-by-default)."
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
        "--attachments-dir",
        default=os.environ.get("SIGNAL_MCP_ATTACHMENTS_DIR", DEFAULT_ATTACHMENTS_DIR),
        help="Directory where signal-cli stores received attachment files. "
        f"(default: {DEFAULT_ATTACHMENTS_DIR}, env: SIGNAL_MCP_ATTACHMENTS_DIR)",
    )
    parser.add_argument(
        "--attachment-transfer",
        default=os.environ.get("SIGNAL_MCP_ATTACHMENT_TRANSFER", "auto"),
        help=(
            "How outbound file attachments are handed to the signal-cli "
            "daemon: 'path' sends local file paths (requires a shared "
            "filesystem with the daemon), 'data-uri' embeds file content as "
            "RFC 2397 data URIs, and 'auto' picks data-uri when --rpc-host "
            "is not a loopback address, path otherwise. "
            "(default: auto, env: SIGNAL_MCP_ATTACHMENT_TRANSFER)"
        ),
    )
    parser.add_argument(
        "--attachment-max-bytes",
        type=int,
        default=int(
            os.environ.get(
                "SIGNAL_MCP_ATTACHMENT_MAX_BYTES",
                str(DEFAULT_ATTACHMENT_MAX_BYTES),
            )
        ),
        help=(
            "Largest attachment (in bytes) accepted: caps a local file encoded "
            "as a data URI (data-uri transfer mode) and an http(s) URL download "
            "(which aborts once the cap is exceeded). "
            f"(default: {DEFAULT_ATTACHMENT_MAX_BYTES} = 25 MB, "
            "env: SIGNAL_MCP_ATTACHMENT_MAX_BYTES)"
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("SIGNAL_MCP_LOG_LEVEL", "INFO"),
        help="Logging verbosity: DEBUG, INFO, WARNING, ERROR, or CRITICAL. "
        "(default: INFO, env: SIGNAL_MCP_LOG_LEVEL)",
    )

    # History buffer caps (A2UI chat surfaces, SPEC-0001).
    history_group = parser.add_argument_group(
        "History buffer",
        "Caps for the in-memory conversation buffer backing A2UI chat surfaces. "
        "All eviction and truncation is silent and never affects message delivery.",
    )
    history_group.add_argument(
        "--history-message-cap",
        type=int,
        default=int(os.environ.get("SIGNAL_MCP_HISTORY_MESSAGE_CAP", "200")),
        help="Maximum messages buffered per conversation (FIFO eviction). "
        "(default: 200, env: SIGNAL_MCP_HISTORY_MESSAGE_CAP)",
    )
    history_group.add_argument(
        "--history-conversation-cap",
        type=int,
        default=int(os.environ.get("SIGNAL_MCP_HISTORY_CONVERSATION_CAP", "50")),
        help="Maximum conversations buffered (LRU eviction). "
        "(default: 50, env: SIGNAL_MCP_HISTORY_CONVERSATION_CAP)",
    )
    history_group.add_argument(
        "--history-text-cap",
        type=int,
        default=int(os.environ.get("SIGNAL_MCP_HISTORY_TEXT_CAP", "4096")),
        help="Maximum stored text bytes per message (truncation with marker). "
        "(default: 4096, env: SIGNAL_MCP_HISTORY_TEXT_CAP)",
    )

    # Reply routing (SPEC-0002). Owns route table bounds + the default
    # agent — kept as its own argument group so it can evolve without
    # colliding with the HTTP server group below.
    routing_group = parser.add_argument_group(
        "Reply routing",
        "Reply routing to the originating agent (SPEC-0002). Active when "
        "--channel runs with --transport http; harmless elsewhere.",
    )
    routing_group.add_argument(
        "--default-agent",
        default=os.environ.get("SIGNAL_MCP_DEFAULT_AGENT", ""),
        help="Agent id that receives unrouted traffic (non-replies, replies "
        "to unknown timestamps, replies to offline agents). Empty fans "
        "unrouted traffic out to every live session. "
        "(env: SIGNAL_MCP_DEFAULT_AGENT)",
    )
    routing_group.add_argument(
        "--route-ttl",
        type=int,
        default=int(os.environ.get("SIGNAL_MCP_ROUTE_TTL", "604800")),
        help="Seconds an outbound-message route stays resolvable. "
        "(default: 604800 = 7 days, env: SIGNAL_MCP_ROUTE_TTL)",
    )
    routing_group.add_argument(
        "--route-max-entries",
        type=int,
        default=int(os.environ.get("SIGNAL_MCP_ROUTE_MAX_ENTRIES", "10000")),
        help="Maximum route table entries (FIFO eviction when full). "
        "(default: 10000, env: SIGNAL_MCP_ROUTE_MAX_ENTRIES)",
    )

    # Central HTTP channel server (SPEC-0002). Auth, bind address, and the
    # route bounds live in separate groups on purpose: they change on
    # different cadences and different stories.
    http_group = parser.add_argument_group(
        "HTTP server",
        "Streamable-HTTP endpoint (--transport http, requires --channel). "
        "Bind to loopback and keep it behind a reverse proxy for TLS when "
        "exposed across hosts.",
    )
    http_group.add_argument(
        "--host",
        default=os.environ.get("SIGNAL_MCP_HOST", "127.0.0.1"),
        help="Bind address for the HTTP transport. "
        "(default: 127.0.0.1, env: SIGNAL_MCP_HOST)",
    )
    http_group.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SIGNAL_MCP_PORT", "8765")),
        help="Port for the HTTP transport. (default: 8765, env: SIGNAL_MCP_PORT)",
    )
    http_group.add_argument(
        "--auth-token",
        default=os.environ.get("SIGNAL_MCP_AUTH_TOKEN", ""),
        help="Bearer token every HTTP request must present. Required for "
        "the HTTP transport unless --allow-unauthenticated is passed with a "
        "loopback bind. Never logged. (env: SIGNAL_MCP_AUTH_TOKEN)",
    )
    http_group.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        default=os.environ.get("SIGNAL_MCP_ALLOW_UNAUTHENTICATED", "").lower()
        in ("1", "true", "yes"),
        help="Run the HTTP transport without auth. Only permitted when the "
        "bind address is loopback — the server can send Signal messages as "
        "the operator, so it must never listen unauthenticated beyond "
        "localhost. (env: SIGNAL_MCP_ALLOW_UNAUTHENTICATED)",
    )

    # S3-compatible attachment storage (self-contained block; issue #20).
    # Credentials are resolved exclusively via the standard AWS chain
    # (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY env vars, shared config files,
    # instance roles) — deliberately no secret-bearing flags here.
    s3_group = parser.add_argument_group(
        "S3 storage",
        "Optional S3-compatible attachment storage (AWS S3, Garage, MinIO, "
        "R2, GCS interop). Setting --s3-bucket enables S3 mode and requires "
        "the signal-mcp[s3] extra (boto3).",
    )
    s3_group.add_argument(
        "--s3-bucket",
        default=os.environ.get("SIGNAL_MCP_S3_BUCKET", ""),
        help="Bucket for attachment storage. Presence enables S3 mode. "
        "(env: SIGNAL_MCP_S3_BUCKET)",
    )
    s3_group.add_argument(
        "--s3-endpoint-url",
        default=os.environ.get("SIGNAL_MCP_S3_ENDPOINT_URL", ""),
        help="Custom S3 endpoint URL for Garage/MinIO/R2/GCS. Empty uses the "
        "AWS default endpoint. (env: SIGNAL_MCP_S3_ENDPOINT_URL)",
    )
    s3_group.add_argument(
        "--s3-region",
        default=os.environ.get("SIGNAL_MCP_S3_REGION", ""),
        help="Region name for the S3 client. Empty defers to the AWS SDK "
        "defaults. (env: SIGNAL_MCP_S3_REGION)",
    )
    s3_group.add_argument(
        "--s3-prefix",
        default=os.environ.get("SIGNAL_MCP_S3_PREFIX", "signal-mcp/"),
        help="Key prefix for uploaded objects. "
        "(default: signal-mcp/, env: SIGNAL_MCP_S3_PREFIX)",
    )
    s3_group.add_argument(
        "--s3-presign-ttl",
        type=int,
        default=int(os.environ.get("SIGNAL_MCP_S3_PRESIGN_TTL", "3600")),
        help="Lifetime of presigned URLs in seconds. "
        "(default: 3600, env: SIGNAL_MCP_S3_PRESIGN_TTL)",
    )
    s3_group.add_argument(
        "--s3-force-path-style",
        action=argparse.BooleanOptionalAction,
        default=_env_tristate("SIGNAL_MCP_S3_FORCE_PATH_STYLE"),
        help="Use path-style S3 addressing (bucket in the URL path). When "
        "neither flag nor env var is given, defaults to on when a custom "
        "--s3-endpoint-url is set (Garage and MinIO need path-style) and "
        "off otherwise. (env: SIGNAL_MCP_S3_FORCE_PATH_STYLE)",
    )

    args = parser.parse_args(argv)

    # --operator is required, but may come from the environment instead of the
    # flag, so validate after parsing rather than with argparse's required=True.
    if not args.operator:
        parser.error("--operator is required (or set SIGNAL_MCP_OPERATOR)")
    # choices isn't enforced for values coming from a default (i.e. the env var).
    if args.transport not in ("sse", "stdio", "http"):
        parser.error(
            f"invalid transport {args.transport!r} "
            "(set SIGNAL_MCP_TRANSPORT to 'sse', 'stdio', or 'http')"
        )
    if args.channel and args.transport == "sse":
        # The bare `--channel` invocation (no --transport) defaults to sse and
        # is coerced to stdio below, exactly as before this spec; only an
        # explicitly requested sse (flag or env) is refused.
        argv = argv if argv is not None else sys.argv[1:]
        explicit_sse = os.environ.get("SIGNAL_MCP_TRANSPORT", "") == "sse" or any(
            (arg == "--transport" and i + 1 < len(argv) and argv[i + 1] == "sse")
            or (arg.startswith("--transport=") and arg.split("=", 1)[1] == "sse")
            for i, arg in enumerate(argv)
        )
        if explicit_sse:
            parser.error(
                "--channel does not support the sse transport: use --transport "
                "stdio (single agent on this machine) or --transport http (the "
                "central channel server with reply routing)"
            )
    if args.transport == "http" and not args.channel:
        parser.error("--transport http requires --channel")
    if args.route_ttl <= 0:
        parser.error(
            f"invalid --route-ttl {args.route_ttl} "
            "(must be a positive number of seconds)"
        )
    if args.route_max_entries <= 0:
        parser.error(
            f"invalid --route-max-entries {args.route_max_entries} "
            "(must be a positive integer)"
        )
    if not args.auth_token and not args.allow_unauthenticated:
        if args.transport == "http":
            parser.error(
                "--transport http requires --auth-token (or "
                "SIGNAL_MCP_AUTH_TOKEN); pass --allow-unauthenticated to run "
                "unauthenticated on loopback"
            )
    if args.allow_unauthenticated and not args.auth_token:
        try:
            loopback = ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            loopback = args.host in ("localhost", "*") and args.host != "*"
        if not loopback:
            parser.error(
                "--allow-unauthenticated is only permitted on a loopback bind "
                f"(got --host {args.host!r})"
            )
    log_level = args.log_level.upper()
    if log_level not in LOG_LEVELS:
        parser.error(
            f"invalid log level {args.log_level!r} "
            f"(choose one of {', '.join(LOG_LEVELS)})"
        )
    if args.s3_presign_ttl <= 0:
        parser.error(
            f"invalid --s3-presign-ttl {args.s3_presign_ttl} "
            "(must be a positive number of seconds)"
        )
    if args.attachment_transfer not in ATTACHMENT_TRANSFER_MODES:
        parser.error(
            f"invalid attachment transfer {args.attachment_transfer!r} "
            "(set SIGNAL_MCP_ATTACHMENT_TRANSFER to "
            f"{', '.join(repr(m) for m in ATTACHMENT_TRANSFER_MODES)})"
        )
    if args.attachment_max_bytes <= 0:
        parser.error(
            f"invalid attachment max bytes {args.attachment_max_bytes!r} "
            "(--attachment-max-bytes must be a positive integer)"
        )

    config.operator = args.operator
    # The MCP's own account defaults to the operator (single-number / Note-to-Self
    # setups); set --account when the agent runs as a distinct number.
    config.account = args.account or args.operator
    config.transport = args.transport
    config.rpc_host = args.rpc_host
    config.rpc_port = args.rpc_port
    config.trusted_recipients = _load_trusted_recipients(args.trusted_recipients)
    config.trusted_senders = _load_trusted_senders(args.trusted_senders)
    config.channel_mode = args.channel
    config.prefix = args.prefix
    config.prompts_dir = Path(args.prompts_dir).expanduser()
    config.log_level = log_level
    config.attachments_dir = os.path.expanduser(args.attachments_dir)
    config.attachment_transfer = args.attachment_transfer
    config.attachment_max_bytes = args.attachment_max_bytes
    config.history_message_cap = args.history_message_cap
    config.history_conversation_cap = args.history_conversation_cap
    config.history_text_cap = args.history_text_cap
    config.default_agent = args.default_agent
    config.route_ttl = args.route_ttl
    config.route_max_entries = args.route_max_entries
    config.http_host = args.host
    config.http_port = args.port
    config.auth_token = args.auth_token
    config.allow_unauthenticated = args.allow_unauthenticated

    # Tri-state path-style: flag/env win when given; otherwise default to
    # path-style whenever a custom endpoint is configured (Garage and MinIO
    # need it), and virtual-hosted addressing for plain AWS.
    force_path_style = args.s3_force_path_style
    if force_path_style is None:
        force_path_style = bool(args.s3_endpoint_url)

    config.s3_bucket = args.s3_bucket
    config.s3_endpoint_url = args.s3_endpoint_url
    config.s3_region = args.s3_region
    config.s3_prefix = args.s3_prefix
    config.s3_presign_ttl = args.s3_presign_ttl
    config.s3_force_path_style = force_path_style

    # Channel mode defaults to the single-agent stdio shape; an explicitly
    # requested HTTP transport is honored (the central channel server).
    if config.channel_mode and config.transport == "sse":
        config.transport = "stdio"

    return config
