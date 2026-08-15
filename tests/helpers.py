"""Shared helpers for the test suite."""

import asyncio
from collections.abc import Awaitable, Coroutine
from typing import Any, TypeVar, cast

T = TypeVar("T")


def run_tool(awaitable: Awaitable[T]) -> T:
    """Run the awaitable returned by calling an ``@mcp.tool`` function.

    fastmcp types ``@mcp.tool`` as returning a ``FunctionTool`` whose call
    yields an ``Awaitable``, while at runtime the decorator hands back the
    original coroutine function untouched. ``asyncio.run`` accepts only a
    ``Coroutine``, so calling a decorated tool directly — which the suite does
    throughout, to exercise handlers without a client — needs the two
    reconciled. The cast is safe precisely because the runtime object is the
    coroutine the annotation obscures.
    """
    return asyncio.run(cast(Coroutine[Any, Any, T], awaitable))
