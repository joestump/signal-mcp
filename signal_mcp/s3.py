"""Async-friendly client for S3-compatible attachment storage.

boto3 is an *optional* dependency (the ``signal-mcp[s3]`` extra) and is only
imported lazily, inside :func:`get_client`, so the server runs fine without
boto3 installed as long as S3 mode is not enabled. All boto3 calls are
blocking, so they run in a thread executor (:func:`asyncio.to_thread`) to stay
friendly to the event loop.

Credentials are resolved exclusively via the standard AWS chain
(``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` environment variables, shared
config/credentials files, instance roles); this module never handles or logs
secrets.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from signal_mcp.config import config
from signal_mcp.parse import MessageResponse

logger = logging.getLogger(__name__)

BOTO3_INSTALL_HINT = (
    "S3 storage is configured (--s3-bucket / SIGNAL_MCP_S3_BUCKET) but boto3 "
    "is not installed. Install the S3 extra: pip install 'signal-mcp[s3]'"
)


class S3Error(Exception):
    """Error configuring or talking to S3-compatible storage."""


# The boto3 client is built once, lazily, on first use.
_client: Any = None


def is_enabled() -> bool:
    """Whether S3 mode is enabled (a bucket is configured)."""
    return bool(config.s3_bucket)


def reset_client() -> None:
    """Drop the cached client so the next call rebuilds it (used by tests)."""
    global _client
    _client = None


def get_client() -> Any:
    """Return the shared boto3 S3 client, building it on first use.

    Raises :class:`S3Error` naming the ``signal-mcp[s3]`` extra when boto3 is
    not installed.
    """
    global _client
    if _client is None:
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as e:
            raise S3Error(BOTO3_INSTALL_HINT) from e

        addressing_style = "path" if config.s3_force_path_style else "virtual"
        client_kwargs: dict[str, Any] = {
            "config": BotoConfig(s3={"addressing_style": addressing_style}),
        }
        if config.s3_endpoint_url:
            client_kwargs["endpoint_url"] = config.s3_endpoint_url
        if config.s3_region:
            client_kwargs["region_name"] = config.s3_region
        _client = boto3.client("s3", **client_kwargs)
        logger.info(
            f"S3 client initialized: bucket={config.s3_bucket!r}, "
            f"endpoint={config.s3_endpoint_url or 'AWS default'}, "
            f"addressing={addressing_style}"
        )
    return _client


def object_key(key: str) -> str:
    """Prepend the configured ``--s3-prefix`` to a relative object key."""
    return f"{config.s3_prefix}{key}"


async def upload_file(path: str, key: str, content_type: str) -> str:
    """Upload the local file at ``path`` to the configured bucket.

    ``key`` is relative to the configured ``--s3-prefix``; the full object key
    actually used is returned.
    """
    client = get_client()
    full_key = object_key(key)

    def _put() -> None:
        with open(path, "rb") as body:
            client.put_object(
                Bucket=config.s3_bucket,
                Key=full_key,
                Body=body,
                ContentType=content_type,
            )

    await asyncio.to_thread(_put)
    logger.debug(f"Uploaded {path} to s3://{config.s3_bucket}/{full_key}")
    return full_key


async def presign(key: str, ttl: int | None = None) -> str:
    """Return a presigned GET URL for ``key`` (relative to ``--s3-prefix``).

    ``ttl`` is the URL lifetime in seconds; it defaults to the configured
    ``--s3-presign-ttl``.
    """
    client = get_client()
    expires = config.s3_presign_ttl if ttl is None else ttl
    url: str = await asyncio.to_thread(
        client.generate_presigned_url,
        "get_object",
        Params={"Bucket": config.s3_bucket, "Key": object_key(key)},
        ExpiresIn=expires,
    )
    return url


def _inbound_key(message_timestamp: int | None, attachment_id: str) -> str:
    """Deterministic object key (relative to ``--s3-prefix``) for an attachment.

    Layout: ``{YYYY}/{MM}/{message-timestamp}-{attachment-id}``. The date
    partition is derived from the message timestamp (milliseconds since the
    epoch, in UTC); a missing timestamp falls back to ``0`` (``1970/01``) so the
    key is always deterministic. Deterministic keys make a re-received
    attachment an idempotent overwrite — no dedupe bookkeeping needed.
    """
    ts = int(message_timestamp or 0)
    when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    return f"{when:%Y}/{when:%m}/{ts}-{attachment_id}"


async def store_inbound_attachments(msg: MessageResponse) -> None:
    """Upload a received message's attachments to S3 and set each ``url``.

    Best-effort and failure-isolated: when S3 mode is disabled, an attachment
    has no resolved local file, or an upload/presign fails, that attachment's
    ``url`` is left ``None`` and delivery proceeds on the local path. An S3
    problem MUST NOT block message flow, so every failure is caught and logged.
    Uploads and presigning run in a thread executor (see :func:`upload_file`
    and :func:`presign`), so they never block the event loop.
    """
    if not is_enabled():
        return
    for att in msg.attachments:
        # Only files that actually resolved on disk can be uploaded; a missing
        # local file keeps its metadata and falls back to the no-path form.
        if not att.path or not att.id:
            continue
        try:
            key = _inbound_key(msg.timestamp, att.id)
            await upload_file(
                att.path, key, att.content_type or "application/octet-stream"
            )
            att.url = await presign(key)
            logger.info(f"Uploaded inbound attachment {att.id!r} to S3")
        except Exception as e:  # noqa: BLE001 — S3 must never block message flow
            logger.warning(
                f"S3 upload failed for inbound attachment {att.id!r}; "
                f"falling back to local path ({e})"
            )


async def validate() -> None:
    """Verify the configured bucket is reachable via ``HeadBucket``.

    Called at startup when S3 mode is enabled. Raises :class:`S3Error` with an
    actionable message (endpoint, bucket, credential/path-style hints) when the
    bucket cannot be reached.
    """
    client = get_client()
    try:
        await asyncio.to_thread(client.head_bucket, Bucket=config.s3_bucket)
    except Exception as e:
        endpoint = config.s3_endpoint_url or "AWS default endpoint"
        path_style = "on" if config.s3_force_path_style else "off"
        raise S3Error(
            f"S3 startup validation failed: HeadBucket on bucket "
            f"{config.s3_bucket!r} ({endpoint}) failed: {e}. Check that the "
            "bucket exists, the endpoint URL is correct, credentials are "
            "available via the standard AWS chain (AWS_ACCESS_KEY_ID/"
            "AWS_SECRET_ACCESS_KEY, shared config files, or an instance "
            f"role), and that path-style addressing (currently {path_style}; "
            "--s3-force-path-style / --no-s3-force-path-style) matches what "
            "the store expects."
        ) from e
    logger.info(f"S3 bucket {config.s3_bucket!r} validated (HeadBucket OK)")
