# Signal MCP

An [MCP](https://github.com/mcp-signal/mcp) integration for [signal-cli](https://github.com/AsamK/signal-cli) that allows AI agents to send and receive Signal messages.

## Features

- Send messages to Signal users
- Send messages to Signal groups
- Receive and parse incoming messages
- Restrict who the server is allowed to message with a trusted-recipients allowlist
- Async support with timeout handling
- Detailed logging

## Prerequisites

This project requires [signal-cli](https://github.com/AsamK/signal-cli) to be installed and configured on your system.

### Installing signal-cli

1. **Install signal-cli**: Follow the [official installation instructions](https://github.com/AsamK/signal-cli/blob/master/README.md#installation)

2. **Register your Signal account**:
   ```bash
   signal-cli -u YOUR_PHONE_NUMBER register
   ```

3. **Verify your account** with the code received via SMS:
   ```bash
   signal-cli -u YOUR_PHONE_NUMBER verify CODE_RECEIVED
   ```

For more detailed setup instructions, see the [signal-cli documentation](https://github.com/AsamK/signal-cli/wiki).

## Installation

```bash
pip install -e .
# or use uv for faster installation
uv pip install -e .
```

## Usage

Run the MCP server:

```bash
./main.py --user-id YOUR_PHONE_NUMBER [--transport {sse|stdio}]
```

### Restricting recipients (trusted recipients)

By default the server can message any recipient. To enforce an allowlist so the
LLM can only message recipients you have approved, pass `--trusted-recipient`
(repeatable) and/or set the comma-separated `SIGNAL_TRUSTED_RECIPIENTS`
environment variable. Values may be user phone numbers (E.164) or group ids:

```bash
# Only allow messaging Alice and one group
./main.py --user-id YOUR_PHONE_NUMBER \
    --trusted-recipient +15555550101 \
    --trusted-recipient GROUP_ID

# Equivalent via environment variable
SIGNAL_TRUSTED_RECIPIENTS="+15555550101,GROUP_ID" ./main.py --user-id YOUR_PHONE_NUMBER
```

Both sources are merged. When the allowlist is non-empty, any `send_message_*`
or `send_reaction_*` call targeting a recipient that is not on it is rejected
with an error before signal-cli is invoked. When no trusted recipients are
configured, enforcement is disabled and every recipient is permitted.

## API

### Tools Available

- `send_message_to_user`: Send a direct message to a Signal user
- `send_message_to_group`: Send a message to a Signal group
- `send_reaction_to_user`: React to a user's message with an emoji (set `remove=True` to undo)
- `send_reaction_to_group`: React to a message in a group with an emoji
- `receive_message`: Wait for and receive messages with timeout support. Returns either a text `message` or a structured `reaction` (emoji + target), so emoji reactions — including "Note to Self" reactions — come through instead of erroring

## Development

This project uses:
- [MCP](https://github.com/mcp-signal/mcp) for agent-API integration
- Modern Python async patterns
- Type annotations throughout
