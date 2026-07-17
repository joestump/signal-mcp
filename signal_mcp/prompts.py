"""Built-in MCP prompts: Signal formatting guidance and reply composition.

Signal renders plain text only, so LLM-composed messages full of markdown come
out riddled with literal ``*`` and ``#`` characters. These prompts give MCP
clients on-demand access to the formatting rules (``signal_style``) and a
ready-made "reply to this message" template (``signal_reply``). The same rules
are baked into channel mode's server instructions via
:data:`SIGNAL_FORMATTING_RULES`.

This module deliberately imports nothing from the rest of the package —
:mod:`signal_mcp.channel` imports :data:`SIGNAL_FORMATTING_RULES` from here and
:mod:`signal_mcp.tools` calls :func:`register_prompts`, so any package import
here would create a cycle.
"""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts.base import UserMessage
from pydantic import Field

SIGNAL_FORMATTING_RULES = """\
Signal formatting rules — Signal renders NO markdown. Asterisks, backticks,
underscores, and # characters are shown literally, so *bold*, `code`,
_italic_, and # headers must never be used in a Signal message.

What works in Signal messages:
• Plain text, with blank lines between sections
• Emoji for structure and emphasis
• UTF-8 glyphs for formatting: bullet •, arrow →, middot ·, dash —, \
checkmark ✅, cross ❌, warning ⚠️
• Bare https:// URLs — Signal auto-links them ([text](url) links do not work)
"""


def register_prompts(mcp: FastMCP) -> None:
    """Register the built-in Signal prompts on ``mcp``.

    Called once from :mod:`signal_mcp.tools` right after the FastMCP instance
    is created, so the prompts (and the prompts capability) are present in
    sse, stdio, and channel modes alike.
    """

    @mcp.prompt(
        name="signal_style",
        description=(
            "Signal plaintext formatting rules for composing messages: "
            "no markdown; plain text, blank lines, emoji, UTF-8 glyphs, "
            "and bare https:// URLs."
        ),
    )
    def signal_style() -> UserMessage:
        """Return the Signal formatting rules as a user-role message."""
        return UserMessage(SIGNAL_FORMATTING_RULES)

    @mcp.prompt(
        name="signal_reply",
        description=(
            "Compose a reply to a received Signal message following the "
            "Signal formatting rules, then send it with send_message_to_user."
        ),
    )
    def signal_reply(
        sender: Annotated[
            str,
            Field(description="Phone number (E.164) of the message sender"),
        ],
        message: Annotated[
            str,
            Field(description="The Signal message text being replied to"),
        ],
    ) -> UserMessage:
        """Render the reply-composition template for a received message."""
        return UserMessage(
            f"Compose a reply to {sender} responding to their Signal message "
            f"below, following the Signal formatting rules. Then send the "
            f"reply with the send_message_to_user tool, using "
            f"user_id={sender}.\n"
            f"\n"
            f"Their message:\n"
            f"{message}\n"
            f"\n"
            f"{SIGNAL_FORMATTING_RULES}"
        )
