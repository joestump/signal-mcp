"""Parsing of signal-cli envelopes into structured message responses."""

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from signal_mcp.config import config

logger = logging.getLogger(__name__)


@dataclass
class Reaction:
    """An emoji reaction to a message."""

    emoji: str | None = None
    target_author: str | None = None
    target_timestamp: int | None = None
    is_remove: bool = False


@dataclass
class Attachment:
    """A file attachment on a received message.

    ``id`` is signal-cli's stored file name (signal-cli >= 0.14.6 stores it
    *with* the extension, e.g. ``0oHirH8e8bm9oPM0NJ3B.png``). ``filename`` is
    the sender's original file name, which may be ``None``. ``path`` is the
    absolute local path to the file inside the configured attachments
    directory, or ``None`` when the file does not exist on disk (the metadata
    is kept either way).
    """

    id: str | None = None
    content_type: str | None = None
    filename: str | None = None
    size: int | None = None
    path: str | None = None


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
    # File attachments on the message; empty when there are none.
    attachments: list[Attachment] = field(default_factory=list)


def _resolve_attachment_path(attachment_id: str, attachments_dir: str) -> str | None:
    """Resolve an attachment id to the file's absolute path, or ``None``.

    The id arrives over the wire, so it is treated as hostile. An id that is
    not a plain file name (contains path separators or is absolute, e.g.
    ``../../etc/hosts`` or ``/etc/hosts``), or whose file resolves — including
    via symlinks — to a location outside ``attachments_dir``, is treated
    exactly like a missing file: the caller keeps the metadata but ``path``
    stays ``None``. A benign id resolves only when the file actually exists
    inside the attachments directory.
    """
    # signal-cli stores attachments flat, so a legitimate id is always a bare
    # file name. Reject anything else (traversal, absolute paths) outright.
    if os.path.basename(attachment_id) != attachment_id:
        logger.warning(
            f"Ignoring attachment id that is not a plain file name: {attachment_id!r}"
        )
        return None

    root = os.path.realpath(attachments_dir)
    candidate = os.path.realpath(os.path.join(root, attachment_id))
    # Belt and braces: even a bare-name id must never escape the attachments
    # directory (e.g. ``..``, or a symlink inside the dir pointing elsewhere).
    if os.path.commonpath([root, candidate]) != root:
        logger.warning(
            f"Ignoring attachment id resolving outside the attachments dir: "
            f"{attachment_id!r}"
        )
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate


def _parse_attachments(raw: Any, attachments_dir: str) -> list[Attachment]:
    """Parse the ``attachments[]`` array of a signal-cli message.

    Each entry looks like ``{"contentType": "image/jpeg", "filename":
    "photo.jpg", "id": "<stored-file-name>", "size": 12345, ...}``. The ``id``
    is the file name signal-cli stored the attachment under inside
    ``attachments_dir``; ``path`` is resolved to that file's absolute path only
    when it actually exists on disk *inside* that directory, and left ``None``
    otherwise (see :func:`_resolve_attachment_path`).
    """
    attachments: list[Attachment] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        attachment_id = item.get("id")
        path: str | None = None
        if attachment_id:
            path = _resolve_attachment_path(str(attachment_id), attachments_dir)
        attachments.append(
            Attachment(
                id=attachment_id,
                content_type=item.get("contentType"),
                filename=item.get("filename"),
                size=item.get("size"),
                path=path,
            )
        )
    return attachments


def _envelope_to_response(
    payload: dict[str, Any], attachments_dir: str | None = None
) -> MessageResponse | None:
    """Turn one signal-cli envelope into a ``MessageResponse``.

    ``payload`` is a single ``{"envelope": {...}, "account": ...}`` object — the
    ``params`` of a JSON-RPC ``receive`` notification (identical to one line of
    ``signal-cli --output=json receive``). Returns ``None`` for envelopes that
    carry nothing actionable (delivery/read receipts, typing indicators, empty
    sync messages). Messages with attachments but no text body *are*
    actionable and produce a response.

    ``attachments_dir`` overrides where attachment files are looked up on
    disk; it defaults to the configured ``config.attachments_dir``.

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

    if attachments_dir is None:
        attachments_dir = config.attachments_dir
    attachments = _parse_attachments(content.get("attachments"), attachments_dir)

    body = content.get("message")
    if body or attachments:
        logger.info(
            f"Parsed message from {sender}"
            + (f" in group {group}" if group else "")
            + (f" with {len(attachments)} attachment(s)" if attachments else "")
        )
        return MessageResponse(
            message=body or None,
            sender_id=sender,
            sender_name=sender_name,
            group_id=group,
            timestamp=timestamp,
            attachments=attachments,
        )

    return None
