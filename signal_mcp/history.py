"""In-memory conversation buffer for A2UI chat surfaces.

This module is **in-memory only, never persisted, and instance-local**.
A server restart yields an empty buffer; concurrent instances legitimately
diverge (different process start times, different outbound sends observed).
The phone is the only complete record — never describe this as an archive.
See ADR-0001 and SPEC-0001 REQ "Per-Instance History Divergence".
"""

from dataclasses import dataclass, field
from typing import Literal

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
