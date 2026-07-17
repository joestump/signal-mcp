---
sidebar_position: 1
---

# Signal MCP

Signal MCP is an [MCP (Model Context Protocol)](https://github.com/mcp-signal/mcp) server that lets AI agents send and receive [Signal](https://signal.org/) messages through [signal-cli](https://github.com/AsamK/signal-cli).

## What it does

- **Send messages** to Signal users and groups
- **Receive and parse** incoming messages, including emoji reactions
- **Claude Channel mode** — push incoming messages to Claude in real time without polling
- **Prefix filtering** — only forward tagged messages in channel mode
- **Async-first** — built on Python asyncio with timeout handling

## Architecture

Signal MCP is a thin JSON-RPC client for a long-running `signal-cli daemon`. Instead of spawning a fresh JVM per request, it connects to a persistent daemon over TCP. That daemon holds the Signal account lock for its lifetime, so:

- calls are instant (no ~2-3s JVM cold start each time)
- concurrent callers no longer fight over the signal-cli account lock

```
┌──────────────┐     MCP (stdio/SSE)     ┌───────────────┐     JSON-RPC (TCP)     ┌─────────────────┐
│  AI Agent    │◄───────────────────────►│  Signal MCP   │◄──────────────────────►│ signal-cli      │
│  (Claude)    │                         │  Server       │                        │ daemon          │
└──────────────┘                         └───────────────┘                        └────────┬────────┘
                                                                                            │
                                                                                    ┌───────▼───────┐
                                                                                    │ Signal servers │
                                                                                    │   (phone)      │
                                                                                    └───────────────┘
```

## Quick start

```bash
# 1. Start the signal-cli daemon (its -a is the ACCOUNT — the number it runs as)
signal-cli -a YOUR_PHONE_NUMBER daemon --tcp 127.0.0.1:7583 \
  --receive-mode on-start --no-receive-stdout

# 2. Run the MCP server (--operator is who it messages; account defaults to operator)
uv run signal-mcp --operator YOUR_PHONE_NUMBER --transport stdio
```

Ready to dive in? Check the [installation guide](./installation.md).
