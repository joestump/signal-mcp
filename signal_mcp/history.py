"""In-memory conversation buffer for A2UI chat surfaces.

This module is **in-memory only, never persisted, and instance-local**.
A server restart yields an empty buffer; concurrent instances legitimately
diverge (different process start times, different outbound sends observed).
The phone is the only complete record — never describe this as an archive.
See ADR-0001 and SPEC-0001 REQ "Per-Instance History Divergence".
"""

import collections
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from signal_mcp.config import config, is_trusted_sender

if TYPE_CHECKING:
    from signal_mcp.parse import MessageResponse

logger = logging.getLogger(__name__)

# The direction of a buffered message relative to the server's account.
Direction = Literal["inbound", "outbound"]

# Per-message stored-text cap (bytes). Longer bodies are truncated at record
# time with an explicit truncation marker. Configurable via CLI/env in a later
# story; the constant lives here so the cap is named, not magic.
DEFAULT_TEXT_CAP = 4096

# Maximum length of sender-controlled metadata strings (filename, content type)
# stored in the buffer. Prevents a hostile sender from inflating memory via
# pathological strings.
METADATA_MAX_LEN = 256

# Truncation marker appended to truncated message text.
TRUNCATION_MARKER = " […]"


@dataclass
class BufferedAttachment:
    """Metadata-only attachment record — no file bytes, no path, no URL.

    Only ``id``, ``filename``, ``content_type``, and ``size`` are stored.
    The local ``path`` and presigned S3 ``url`` are deliberately excluded:
    paths may be deleted and presigned URLs expire, so a thread rendered
    hours or days into a process's life would show dead links. Surfaces
    render name/type/size only, keeping payload size independent of media
    size (SPEC-0001 REQ "A2UI Envelope Contract").
    """

    id: str | None = None
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None


@dataclass
class BufferedReaction:
    """An emoji reaction attached to a buffered message."""

    emoji: str
    author: str
    author_name: str | None = None


@dataclass
class BufferedMessage:
    """A single message recorded in the conversation buffer.

    ``conversation_key`` is the group id for group messages, otherwise the
    counterparty's number (see :func:`conversation_key`). ``direction``
    distinguishes the server's own sends (``"outbound"``) from received
    traffic (``"inbound"``). ``truncated`` is set when the stored text was
    capped at :data:`DEFAULT_TEXT_CAP`.
    """

    conversation_key: str
    direction: Direction
    sender_id: str | None
    sender_name: str | None
    text: str | None
    timestamp: int | None
    attachments: list[BufferedAttachment] = field(default_factory=list)
    reactions: list[BufferedReaction] = field(default_factory=list)
    truncated: bool = False


def conversation_key(
    *,
    group_id: str | None,
    sender_id: str | None,
    destination: str | None,
    account: str,
) -> str | None:
    """Compute the conversation key for a piece of Signal traffic.

    The key matches how a phone groups threads:

    - A non-empty ``group_id`` wins and is returned as-is.
    - Otherwise the **counterparty's** number is returned:
      ``destination`` when set and not the account's own number (outbound
      and sync-sent DMs), else ``sender_id`` when set and not the account's
      own number (inbound DMs).
    - When everything available equals the account (Note-to-Self:
      sender == destination == account), the account's number is returned.
    - Returns ``None`` when there is nothing to key on; callers treat that
      as "do not record".

    This function is pure — no globals, no config reads, no I/O — so it is
    unit-testable in isolation.
    """
    if group_id:
        return group_id

    if destination and destination != account:
        return destination

    if sender_id and sender_id != account:
        return sender_id

    # Note-to-Self: at least one of sender or destination is the account,
    # and neither is a different number. Key on the account so the thread
    # appears under the operator's own number.
    if account and (sender_id == account or destination == account):
        return account

    return None


def _truncate_text(text: str | None, cap: int) -> tuple[str | None, bool]:
    """Truncate *text* to at most *cap* bytes, returning (text, truncated).

    Slices at the byte boundary with ``errors="ignore"`` so a multi-byte
    character is never split. Appends :data:`TRUNCATION_MARKER` when
    truncation occurs.
    """
    if text is None:
        return None, False
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text, False
    truncated = encoded[:cap].decode("utf-8", errors="ignore")
    return truncated + TRUNCATION_MARKER, True


def _clamp_metadata(value: str | None) -> str | None:
    """Truncate a sender-controlled metadata string to METADATA_MAX_LEN bytes."""
    if value is None:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) <= METADATA_MAX_LEN:
        return value
    return encoded[:METADATA_MAX_LEN].decode("utf-8", errors="ignore")


class ConversationBuffer:
    """Bounded in-memory conversation buffer.

    Backed by ``OrderedDict[str, deque[BufferedMessage]]``:

    - **Per-conversation cap (FIFO)**: each deque has ``maxlen`` set to
      ``config.history_message_cap``, so overflow evicts the oldest message
      automatically.
    - **Conversation cap (LRU)**: after inserting a new key, conversations
      beyond ``config.history_conversation_cap`` are evicted
      least-recently-active first (``OrderedDict.popitem(last=False)``).
    - **Text cap**: stored text is truncated at ``config.history_text_cap``
      bytes with a visible truncation marker.

    All eviction and truncation is silent (debug-logged) and **never
    raises** — a failure to record must not break message delivery (SPEC-0001
    REQ "Error Handling Standards").

    Channel-mode *prefix* filtering is deliberately **not** applied here —
    a trusted message without the ``cc`` prefix is still real conversation
    content and belongs in the thread, even though it is not forwarded to
    the model.
    """

    def __init__(self) -> None:
        self._conversations: collections.OrderedDict[
            str, collections.deque[BufferedMessage]
        ] = collections.OrderedDict()

    def record(self, message: BufferedMessage) -> None:
        """Record a message into the buffer.

        This is the single low-level entry point. It applies text
        truncation, attachment metadata clamping, FIFO per-conversation
        eviction, and LRU cross-conversation eviction. Never raises —
        any internal failure is caught, logged at warning, and swallowed.
        """
        try:
            key = message.conversation_key
            if not key:
                return

            # Apply text cap.
            text, truncated = _truncate_text(message.text, config.history_text_cap)
            message.text = text
            message.truncated = truncated

            # Clamp attachment metadata.
            clamped: list[BufferedAttachment] = []
            for att in message.attachments:
                clamped.append(
                    BufferedAttachment(
                        id=att.id,
                        filename=_clamp_metadata(att.filename),
                        content_type=_clamp_metadata(att.content_type),
                        size=att.size,
                    )
                )
            message.attachments = clamped

            # Get or create the conversation deque (FIFO-bounded).
            if key not in self._conversations:
                # LRU: make room *before* inserting, so the conversation being
                # recorded is never the one evicted. The floor of one keeps a
                # zero or negative cap degrading to "keep only the newest
                # conversation" instead of evicting the key we are about to
                # append to.
                cap = max(1, config.history_conversation_cap)
                while len(self._conversations) >= cap:
                    evicted_key, _ = self._conversations.popitem(last=False)
                    logger.debug(
                        f"History buffer: evicted conversation {evicted_key!r} "
                        "(LRU cap)"
                    )
                self._conversations[key] = collections.deque(
                    maxlen=config.history_message_cap
                )

            self._conversations[key].append(message)
            self._conversations.move_to_end(key)

            if truncated:
                logger.debug(
                    f"History buffer: truncated message text for {key!r} "
                    f"(cap={config.history_text_cap})"
                )
        except Exception:
            logger.warning("History buffer: failed to record message", exc_info=True)

    def snapshot(self, key: str) -> list[BufferedMessage]:
        """Return a point-in-time copy of a conversation's messages.

        Returns an empty list when the conversation has no buffered
        messages. The copy is a plain list so the caller is immune to
        mid-iteration mutation.
        """
        deque_ = self._conversations.get(key)
        if deque_ is None:
            return []
        return list(deque_)

    def conversation_keys(self) -> list[str]:
        """Return all buffered conversation keys, most-recently-active last."""
        return list(self._conversations.keys())

    def record_reaction(
        self,
        *,
        conversation_key: str,
        emoji: str | None,
        author: str | None,
        author_name: str | None,
        target_author: str | None,
        target_timestamp: int | None,
        is_remove: bool,
        trusted_check: bool = True,
    ) -> None:
        """Attach a reaction to the buffered message it targets.

        Looks up the message in *conversation_key* whose ``(sender_id,
        timestamp)`` matches ``(target_author, target_timestamp)``. If no
        match is found the call is a silent no-op — a reaction can
        legitimately target a message that predates process start or has
        been evicted.

        On a match with ``is_remove=False``: **replace by author** — remove
        any existing reaction from the same *author* on that message, then
        append. One reaction per author per message (Signal's semantics),
        so reaction growth is bounded by distinct reacting authors.

        On a match with ``is_remove=True``: remove the matching
        emoji-by-author from the target message's reactions. Removing
        something not present is a no-op.

        When *trusted_check* is True and *author* is not a trusted sender,
        the reaction is dropped at record time — same trust boundary as
        :func:`record_inbound`.

        Never raises.
        """
        try:
            if trusted_check and author is not None:
                if not is_trusted_sender(author):
                    return

            if emoji is None:
                return

            deque_ = self._conversations.get(conversation_key)
            if deque_ is None:
                return

            for msg in deque_:
                if msg.sender_id == target_author and msg.timestamp == target_timestamp:
                    if is_remove:
                        msg.reactions = [r for r in msg.reactions if r.author != author]
                    else:
                        # Replace by author: remove existing, then append.
                        msg.reactions = [r for r in msg.reactions if r.author != author]
                        msg.reactions.append(
                            BufferedReaction(
                                emoji=emoji,
                                author=author or "",
                                author_name=author_name,
                            )
                        )
                    return
            # No match found — silent no-op.
        except Exception:
            logger.warning("History buffer: failed to record reaction", exc_info=True)


# Module-level singleton for the tap stories to import.
buffer = ConversationBuffer()


def record_inbound(msg: "MessageResponse") -> None:
    """Record an inbound MessageResponse into the buffer, trust-gated.

    Applies ``is_trusted_sender`` — untrusted authors are silently dropped
    so a surface can never become a side channel around the existing gate.
    Never raises. Does not implement reaction attachment (separate issue).
    """
    try:
        if not is_trusted_sender(msg.sender_id):
            return

        key = conversation_key(
            group_id=msg.group_id,
            sender_id=msg.sender_id,
            destination=getattr(msg, "destination", None),
            account=config.account,
        )
        if key is None:
            return

        attachments = [
            BufferedAttachment(
                id=a.id,
                filename=a.filename,
                content_type=a.content_type,
                size=a.size,
            )
            for a in msg.attachments
        ]

        buffer.record(
            BufferedMessage(
                conversation_key=key,
                direction="inbound",
                sender_id=msg.sender_id,
                sender_name=msg.sender_name,
                text=msg.message,
                timestamp=msg.timestamp,
                attachments=attachments,
            )
        )
    except Exception:
        logger.warning("History buffer: record_inbound failed", exc_info=True)


def record_outbound(
    *,
    text: str | None,
    timestamp: int | None,
    target: str,
    is_group: bool = False,
) -> None:
    """Record an outbound send into the buffer, attributed to the account.

    ``target`` is the recipient (user number or group id). ``is_group``
    determines the conversation key. Never raises.
    """
    try:
        key = conversation_key(
            group_id=target if is_group else None,
            sender_id=config.account,
            destination=target if not is_group else None,
            account=config.account,
        )
        if key is None:
            return

        buffer.record(
            BufferedMessage(
                conversation_key=key,
                direction="outbound",
                sender_id=config.account,
                sender_name=None,
                text=text,
                timestamp=timestamp,
            )
        )
    except Exception:
        logger.warning("History buffer: record_outbound failed", exc_info=True)
