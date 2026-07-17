"""JSON-RPC client for a long-running ``signal-cli daemon`` (TCP)."""

import asyncio
import contextlib
import json
import logging
from typing import Any

from signal_mcp.config import config
from signal_mcp.parse import MessageResponse, _envelope_to_response

logger = logging.getLogger(__name__)


class SignalError(Exception):
    """Base exception for Signal-related errors."""


class SignalCLIError(SignalError):
    """Exception raised when a signal-cli JSON-RPC call fails."""


class SignalDisconnectedError(SignalCLIError):
    """Raised by :meth:`SignalRpcClient.next_message` when the connection drops.

    Distinct from a healthy idle timeout (which returns ``None``): a disconnect
    is an error condition that callers should back off on before reconnecting.
    A caller that merely wants to keep polling can treat it like a timeout.
    """


class UntrustedRecipientError(SignalError):
    """Raised when a send is attempted to a recipient not on the allowlist."""


class _Disconnect:
    """Sentinel pushed into the message queue to wake :meth:`next_message`.

    When the connection drops, a blocked ``next_message`` caller must wake
    promptly instead of waiting out its (possibly hour-long) receive timeout.
    Teardown enqueues this sentinel; the waiter turns it into a
    :class:`SignalDisconnectedError` and the next call reconnects.
    """


_DISCONNECT = _Disconnect()


class SignalRpcClient:
    """A persistent JSON-RPC client for a ``signal-cli daemon`` (TCP).

    One connection is opened lazily and kept alive. A background reader task
    routes responses back to their callers by ``id`` and funnels ``receive``
    notifications into a queue for :meth:`next_message`. If the connection drops
    it is transparently re-established on the next call.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._messages: asyncio.Queue[MessageResponse | _Disconnect] = asyncio.Queue()
        self._id = 0
        self._connect_lock = asyncio.Lock()

    @property
    def _connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Ensure the connection to the daemon is open."""
        await self._ensure_connected()

    async def _ensure_connected(self) -> None:
        if self._connected:
            return
        async with self._connect_lock:
            if self._connected:
                return
            logger.info(f"Connecting to signal-cli daemon at {self.host}:{self.port}")
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    self.host, self.port
                )
            except OSError as e:
                raise SignalCLIError(
                    f"Cannot reach signal-cli daemon at {self.host}:{self.port} "
                    f"({e}). Is the daemon running? "
                    f"(macOS: `signal-daemon status`)"
                )
            # A previous connection's teardown may have left disconnect
            # sentinels in the queue. Drop them now that we have reconnected
            # cleanly (a disconnect only matters when a caller is actively
            # blocked or the reconnect itself fails); otherwise a stale sentinel
            # would surface a spurious disconnect on the next next_message.
            self._drain_disconnect_sentinels()
            self._reader_task = asyncio.create_task(self._read_loop())
            logger.info("Connected to signal-cli daemon")

    def _drain_disconnect_sentinels(self) -> None:
        """Remove any queued :data:`_DISCONNECT` sentinels, preserving messages.

        Safe to call only when no reader task is producing (inside the connect
        lock, after the old reader has torn down and before the new one starts),
        so there is no concurrent enqueue to race.
        """
        kept: list[MessageResponse] = []
        while True:
            try:
                item = self._messages.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not isinstance(item, _Disconnect):
                kept.append(item)
        for message in kept:
            self._messages.put_nowait(message)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break  # EOF — daemon closed the connection
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(f"Skipping non-JSON line: {line[:80]!r}")
                    continue

                if "method" in obj:
                    # A notification. We only care about incoming messages.
                    if obj.get("method") == "receive":
                        parsed = _envelope_to_response(obj.get("params") or {})
                        if parsed is not None:
                            self._messages.put_nowait(parsed)
                    continue

                # Otherwise it's a response to one of our requests.
                rid = obj.get("id")
                fut = self._pending.pop(rid, None)
                if fut is None or fut.done():
                    continue
                if obj.get("error") is not None:
                    fut.set_exception(SignalCLIError(json.dumps(obj["error"])))
                else:
                    fut.set_result(obj.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — surface, then tear down cleanly
            logger.warning(f"signal-cli daemon reader loop ended: {e}")
        finally:
            self._teardown(SignalCLIError("signal-cli daemon connection closed"))

    def _teardown(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._writer = None
        self._reader = None
        # Wake any blocked next_message waiter so it can reconnect promptly
        # instead of waiting out its (possibly hour-long) receive timeout.
        self._messages.put_nowait(_DISCONNECT)

    async def close(self) -> None:
        """Cancel the reader task and close the connection; call on shutdown.

        Cancelling the reader task runs its ``finally`` (which tears down the
        connection and fails any pending requests). When no reader is running,
        teardown is invoked directly so a never-connected client still releases
        its state. Safe to call more than once.
        """
        task = self._reader_task
        self._reader_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Release the writer/pending state even if the reader task was
        # cancelled before it entered its try/finally (so its teardown never
        # ran), or if the client never connected at all.
        if self._writer is not None:
            self._teardown(SignalCLIError("signal-cli daemon client closed"))

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        """Issue a JSON-RPC request and await its result."""
        await self._ensure_connected()
        assert self._writer is not None

        self._id += 1
        rid = self._id
        req: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params is not None:
            req["params"] = params

        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut

        logger.debug(f"JSON-RPC -> {method} (id={rid})")
        try:
            self._writer.write((json.dumps(req) + "\n").encode())
            await self._writer.drain()
        except OSError as e:
            # The socket broke mid-write. Drop our own pending future (the
            # reader loop's teardown will handle any others) and surface a
            # typed error rather than a raw OSError.
            self._pending.pop(rid, None)
            raise SignalCLIError(
                f"Failed to send {method} to signal-cli daemon: {e}"
            ) from e

        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise SignalCLIError(f"signal-cli daemon timed out on {method}")

    async def next_message(self, timeout: float) -> MessageResponse | None:
        """Wait up to ``timeout`` seconds for the next actionable message.

        Returns ``None`` on a genuine idle timeout. Raises
        :class:`SignalDisconnectedError` when the connection drops mid-wait (a
        :data:`_DISCONNECT` sentinel is enqueued by teardown) — distinguishing
        a healthy quiet period from a dropped connection so callers can back off
        on the latter instead of hot-looping through instant reconnects. The
        next call transparently reconnects.
        """
        await self._ensure_connected()
        try:
            item = await asyncio.wait_for(self._messages.get(), timeout)
        except asyncio.TimeoutError:
            return None
        if isinstance(item, _Disconnect):
            raise SignalDisconnectedError("signal-cli daemon connection dropped")
        return item


# Global JSON-RPC client, created lazily from the global config.
client: SignalRpcClient | None = None


def get_client() -> SignalRpcClient:
    """Return the shared RPC client, creating it from config on first use."""
    global client
    if client is None:
        client = SignalRpcClient(config.rpc_host, config.rpc_port)
    return client
