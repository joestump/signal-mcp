"""JSON-RPC client for a long-running ``signal-cli daemon`` (TCP)."""

import asyncio
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


class UntrustedRecipientError(SignalError):
    """Raised when a send is attempted to a recipient not on the allowlist."""


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
        self._messages: asyncio.Queue[MessageResponse] = asyncio.Queue()
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
            self._reader_task = asyncio.create_task(self._read_loop())
            logger.info("Connected to signal-cli daemon")

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
        self._writer.write((json.dumps(req) + "\n").encode())
        await self._writer.drain()

        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise SignalCLIError(f"signal-cli daemon timed out on {method}")

    async def next_message(self, timeout: float) -> MessageResponse | None:
        """Wait up to ``timeout`` seconds for the next actionable message."""
        await self._ensure_connected()
        try:
            return await asyncio.wait_for(self._messages.get(), timeout)
        except asyncio.TimeoutError:
            return None


# Global JSON-RPC client, created lazily from the global config.
client: SignalRpcClient | None = None


def get_client() -> SignalRpcClient:
    """Return the shared RPC client, creating it from config on first use."""
    global client
    if client is None:
        client = SignalRpcClient(config.rpc_host, config.rpc_port)
    return client
