"""Smoke test: launch the Signal MCP server over stdio and exercise its tools.

Run from the repository root with a signal-cli daemon already running:

    SENDER_NUMBER=+15551234567 RECEIVER_NUMBER=+15555550101 \
        uv run --extra test python examples/check_mcp.py

Environment variables (a ``.env`` file is loaded when python-dotenv is
installed):

- ``SENDER_NUMBER`` — the Signal account the server runs as (``--user-id``).
- ``RECEIVER_NUMBER`` — where the test message is sent.
"""

import asyncio
import json
import os

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import CallToolResult, TextContent

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional (part of the test extra)
    pass
else:
    load_dotenv()


def _text_blocks(result: CallToolResult) -> list[str]:
    """Extract the text payloads from a tool result's content blocks."""
    return [block.text for block in result.content if isinstance(block, TextContent)]


async def main() -> None:
    # Configure the stdio client to launch the server as a subprocess.
    server_params = StdioServerParameters(
        command="python",
        args=[
            "signal_mcp/main.py",
            "--user-id",
            os.environ["SENDER_NUMBER"],
            "--transport",
            "stdio",
        ],
    )
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # List available tools.
            response = await session.list_tools()
            print("Tools:", ", ".join(tool.name for tool in response.tools))

            # Call a tool to send a message. Failures surface as tool errors
            # (isError) with the reason in the content blocks.
            send_result = await session.call_tool(
                "send_message_to_user",
                {
                    "message": "Hello from MCP stdio client!",
                    "user_id": os.environ["RECEIVER_NUMBER"],
                },
            )
            status = "failed" if send_result.isError else "ok"
            print(f"Send {status}: {'; '.join(_text_blocks(send_result))}")

            # Receive a message, waiting up to ten seconds.
            print("Waiting up to 10s for a message...")
            receive_result = await session.call_tool(
                "receive_message",
                {"timeout": 10},  # ten-second timeout
            )
            if receive_result.isError:
                print(f"Receive failed: {'; '.join(_text_blocks(receive_result))}")
                return

            # The tool returns a MessageResponse serialized as JSON text.
            for text in _text_blocks(receive_result):
                payload = json.loads(text)
                message = payload.get("message")
                sender = payload.get("sender_id")
                group = payload.get("group_id")
                if message and sender:
                    print(f"Received message from {sender}: {message}")
                    if group:
                        print(f"In group: {group}")
                else:
                    print("No message received before the timeout")


if __name__ == "__main__":
    asyncio.run(main())
