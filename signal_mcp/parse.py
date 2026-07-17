"""Parsing of signal-cli envelopes into structured message responses."""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Reaction:
    """An emoji reaction to a message."""

    emoji: str | None = None
    target_author: str | None = None
    target_timestamp: int | None = None
    is_remove: bool = False


@dataclass
class MessageResponse:
    """Structured result for received messages and reactions.

    A plain text message populates ``message``. An emoji reaction populates
    ``reaction`` instead (``message`` stays ``None``), so callers can tell the
    two apart.
    """

    message: str | None = None
    sender_id: str | None = None
    # The sender's profile/contact name (signal-cli ``sourceName``), when known.
    sender_name: str | None = None
    # Internal id of the group the message was posted in, else ``None``.
    group_id: str | None = None
    # Timestamp of the received message — pass it back as ``target_timestamp``
    # to react to this message.
    timestamp: int | None = None
    reaction: Reaction | None = None


def _envelope_to_response(payload: dict[str, Any]) -> MessageResponse | None:
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
    sender_name = envelope.get("sourceName")

    # A body/reaction lives on dataMessage (messages from others) or on
    # syncMessage.sentMessage (anything you sent from another linked device,
    # including reactions in "Note to Self").
    data_message = envelope.get("dataMessage") or {}
    sent_message = (envelope.get("syncMessage") or {}).get("sentMessage") or {}
    content: dict[str, Any] = data_message or sent_message
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
            sender_name=sender_name,
            group_id=group,
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
            sender_name=sender_name,
            group_id=group,
            timestamp=timestamp,
        )

    return None
