---
status: draft
date: 2026-09-03
implements: [ADR-0002]
requires: [SPEC-0001]
---

# SPEC-0002: Agent Reply Routing

## Overview

When several agents send the operator Signal messages through one signal-cli daemon, a reply the operator types on the phone is delivered to the agent that sent the message being replied to, and to no other agent. signal-mcp achieves this by running once, centrally, as a streamable-HTTP MCP server in channel mode: each connected agent identifies itself per session, every outbound send is recorded in a bounded in-memory route table keyed by its Signal timestamp, and an inbound reply's `quote` is resolved against that table to pick the recipient session. Traffic that cannot be routed goes to a configured default agent with the routing outcome disclosed in the notification's `meta`. See ADR-0002.

The route table is deliberately separate from the SPEC-0001 conversation buffer, whose per-instance contract is unchanged by this spec.

## Requirements

### Requirement: Quote Parsing

The envelope parser SHALL populate a `quote` field on `MessageResponse` whenever the parsed `dataMessage` or sync `sentMessage` carries a `quote` object. The field SHALL carry the quoted message's timestamp (`quote.id`), its author (`quote.author`, falling back to `quote.authorNumber`), and the quoted text truncated to at most 256 bytes at a UTF-8 character boundary. A message without a `quote` object SHALL have `quote` set to `None`. Quote parsing MUST NOT change whether an envelope is considered actionable: an envelope that was not returned before this spec is still not returned.

#### Scenario: Reply carries a quote

- **WHEN** an inbound `dataMessage` contains `quote: {id: 1700, author: "+15551230000", text: "deploy finished"}`
- **THEN** the returned `MessageResponse.quote` has timestamp 1700, author `+15551230000`, and text `deploy finished`

#### Scenario: Plain message has no quote

- **WHEN** an inbound `dataMessage` has no `quote` object
- **THEN** `MessageResponse.quote` is `None` and every other field is populated exactly as before

#### Scenario: Oversized quoted text is truncated

- **WHEN** a reply quotes a message whose text exceeds 256 bytes
- **THEN** `quote.text` is truncated at a character boundary to at most 256 bytes and parsing succeeds

### Requirement: Reply Metadata in Channel Notifications

In every channel mode (stdio and HTTP), a channel notification for a message whose `quote` is set SHALL include `in_reply_to_timestamp`, `in_reply_to_author`, and `in_reply_to_text` in `meta`, all as strings, with `in_reply_to_text` omitted when the quoted text is empty. The channel instructions SHALL describe these keys and state that a reply whose `in_reply_to_author` equals the account is a reply to one of the agent's (or a sibling agent's) own messages. Notifications for messages without a quote MUST NOT carry these keys.

#### Scenario: Reply notification is annotated

- **WHEN** the channel forwarder forwards a trusted sender's reply quoting timestamp 1700 from the account
- **THEN** the notification `meta` contains `in_reply_to_timestamp: "1700"`, `in_reply_to_author: <account>`, and `in_reply_to_text` with the quoted text

#### Scenario: Non-reply notification is unchanged

- **WHEN** the channel forwarder forwards a message with no quote
- **THEN** `meta` contains none of the `in_reply_to_*` keys and is otherwise identical to the pre-spec shape

### Requirement: HTTP Channel Transport

The server SHALL support `--channel` combined with `--transport http`, running as a streamable-HTTP MCP server that declares the `claude/channel` experimental capability and forwards inbound traffic as `notifications/claude/channel` on each recipient session's server-to-client stream. `--channel` with `--transport stdio` SHALL behave exactly as before this spec. `--channel` with `--transport sse` MUST be rejected at startup with an actionable error. The HTTP transport SHALL bind to `127.0.0.1` on port `8765` unless `--host`/`--port` (env `SIGNAL_MCP_HOST`/`SIGNAL_MCP_PORT`) say otherwise. Channel mode MUST NOT force the transport to stdio when `http` was requested.

#### Scenario: HTTP channel server starts

- **WHEN** the server is started with `--channel --transport http --auth-token T`
- **THEN** it listens on the configured host and port, and an `initialize` response advertises the `claude/channel` experimental capability

#### Scenario: stdio channel mode is untouched

- **WHEN** the server is started with `--channel` and no `--transport` (or `--transport stdio`)
- **THEN** it runs over stdio with the existing single-consumer forwarder and no routing behavior

#### Scenario: SSE with channel is refused

- **WHEN** the server is started with `--channel --transport sse`
- **THEN** it exits non-zero at argument parsing with a message naming `http` and `stdio` as the valid channel transports

### Requirement: Agent Session Identity

In HTTP channel mode the server SHALL identify each MCP session by the value of the `X-Signal-Agent-Id` request header observed on that session's `initialize` request, falling back to the MCP session id when the header is absent or blank. The server SHALL maintain a registry of live sessions per agent id, adding a session at `initialize` and removing it when the session ends. Agent ids SHALL be treated as opaque strings, trimmed, and capped at 128 bytes. More than one live session MAY share an agent id; all of them are recipients for that agent.

#### Scenario: Session registers with a header

- **WHEN** a client connects with `X-Signal-Agent-Id: crush-signal` and completes `initialize`
- **THEN** the session registry maps `crush-signal` to that session

#### Scenario: Session without a header

- **WHEN** a client connects with no `X-Signal-Agent-Id` header
- **THEN** the session is registered under its MCP session id and can still receive unrouted traffic

#### Scenario: Session ends

- **WHEN** a registered session disconnects or its transport closes
- **THEN** the registry no longer lists it, and subsequent dispatch to that agent id treats it as offline if no other session remains

### Requirement: Route Registry

The server SHALL maintain an in-memory `RouteTable` mapping an outbound Signal timestamp to the agent id of the session that sent it. In HTTP channel mode, `send`, `send_message_to_user`, and `send_message_to_group` SHALL record a route after the daemon RPC succeeds, using the calling session's agent id and the `timestamp` from the RPC result. The table SHALL be bounded by a TTL (`--route-ttl`, env `SIGNAL_MCP_ROUTE_TTL`, default 604800 seconds) and a maximum entry count (`--route-max-entries`, env `SIGNAL_MCP_ROUTE_MAX_ENTRIES`, default 10000), evicting expired entries lazily and the oldest entries first when full. Entries SHALL store only the agent id, the conversation key, a text preview of at most 256 bytes, and the record time. The table MUST NOT be persisted to disk and MUST NOT be derived from or merged into the SPEC-0001 conversation buffer. Recording MUST NOT raise or fail a send.

#### Scenario: Send records a route

- **WHEN** a session identified as `agent-a` calls `send` and the daemon returns `{timestamp: 1700}`
- **THEN** `RouteTable.lookup(1700)` returns an entry whose agent id is `agent-a`

#### Scenario: Expired route is forgotten

- **WHEN** an entry older than the TTL is looked up
- **THEN** the lookup returns `None` and the entry is removed

#### Scenario: Full table evicts oldest

- **WHEN** the table holds the maximum number of entries and a new route is recorded
- **THEN** the oldest entry is evicted, the new entry is stored, and the send that triggered it succeeds

#### Scenario: Restart clears routes

- **WHEN** the server restarts and a reply quoting a pre-restart timestamp arrives
- **THEN** the lookup misses and the reply is dispatched as unrouted traffic

### Requirement: Reply Dispatch

In HTTP channel mode, for each inbound message that passes the existing trusted-sender gate and prefix filter, the forwarder SHALL resolve recipients as follows, in order:

1. If `quote` is set, `quote.author` equals the account, and `RouteTable.lookup(quote.id)` hits an agent with at least one live session: deliver to that agent's sessions only, with `meta.route_status: "routed"` and `meta.routed_agent` set to the agent id.
2. If the lookup hits an agent with no live session: deliver as unrouted traffic with `meta.route_status: "agent_offline"` and `meta.routed_agent` set to the agent id.
3. If `quote` is set and the lookup misses: deliver as unrouted traffic with `meta.route_status: "unknown"`.
4. Otherwise: deliver as unrouted traffic with `meta.route_status: "unrouted"`.

Sessions that are not recipients MUST NOT receive the notification. Prefix filtering SHALL apply to routed replies exactly as it applies today; a reply that fails the prefix check is dropped for every session. Delivery failures on one session MUST NOT prevent delivery to another, and MUST be logged with the session's agent id.

#### Scenario: Reply reaches only the originating agent

- **WHEN** sessions for `agent-a` and `agent-b` are live, the route table maps 1700 to `agent-a`, and a trusted reply quoting 1700 from the account arrives
- **THEN** `agent-a`'s session receives the notification with `route_status: "routed"` and `agent-b`'s session receives nothing

#### Scenario: Originating agent is offline

- **WHEN** the route table maps 1700 to `agent-a`, no session for `agent-a` is live, and a reply quoting 1700 arrives
- **THEN** the notification is delivered per the unrouted rules with `route_status: "agent_offline"` and `routed_agent: "agent-a"`

#### Scenario: Reply to an unknown message

- **WHEN** a reply quotes a timestamp with no route table entry
- **THEN** the notification is delivered per the unrouted rules with `route_status: "unknown"` and the `in_reply_to_*` keys still present

#### Scenario: Reply from a different author

- **WHEN** a reply quotes a message whose `quote.author` is not the account
- **THEN** the message is treated as unrouted traffic, not routed, even if the timestamp happens to exist in the table

### Requirement: Reaction Dispatch

Inbound emoji reactions SHALL be dispatched with the same rules as replies, using `reaction.target_author` in place of `quote.author` and `reaction.target_timestamp` in place of `quote.id`. Reaction notifications SHALL carry the existing reaction `meta` keys plus `route_status` and, when applicable, `routed_agent`.

#### Scenario: Reaction reaches only the originating agent

- **WHEN** the route table maps 1700 to `agent-a` and the operator reacts 👍 to message 1700 authored by the account
- **THEN** only `agent-a`'s sessions receive the reaction event, with `route_status: "routed"`

#### Scenario: Reaction to an unknown message

- **WHEN** a reaction targets a timestamp with no route entry
- **THEN** the event is delivered as unrouted traffic with `route_status: "unknown"`

### Requirement: Unrouted Traffic and the Default Agent

The server SHALL accept `--default-agent AGENT_ID` (env `SIGNAL_MCP_DEFAULT_AGENT`). When set, unrouted traffic SHALL be delivered to every live session of that agent id and to no other session. When the default agent has no live session, or when no default agent is configured, unrouted traffic SHALL be delivered to every live session. Unrouted deliveries SHALL carry the `route_status` value that produced them and, for replies, the `in_reply_to_*` keys so the receiving agent can act with context.

#### Scenario: Default agent receives a plain message

- **WHEN** `--default-agent crush-signal` is set, sessions for `crush-signal` and `deploy-bot` are live, and a non-reply message arrives
- **THEN** only `crush-signal`'s session receives it, with `route_status: "unrouted"`

#### Scenario: No default agent configured

- **WHEN** no default agent is configured and two sessions are live
- **THEN** a non-reply message is delivered to both sessions

#### Scenario: Default agent offline

- **WHEN** `--default-agent crush-signal` is set but no `crush-signal` session is live
- **THEN** unrouted traffic is delivered to every other live session

### Requirement: Single Read Receipt

In HTTP channel mode the server SHALL send at most one read receipt per forwarded inbound message, after dispatch, regardless of how many sessions received it, and SHALL send none when no session received it. Receipts MUST NOT be sent for sync-sent envelopes or reactions, as today.

#### Scenario: Receipt is sent once for a fan-out

- **WHEN** a non-reply message is delivered to three live sessions
- **THEN** exactly one `sendReceipt` RPC is issued for that message

#### Scenario: No recipients, no receipt

- **WHEN** a message is dropped by the prefix filter
- **THEN** no `sendReceipt` RPC is issued

### Requirement: Routing Disclosure in Channel Instructions

The channel instructions served in HTTP channel mode SHALL state the session's own agent id, explain that a notification with `route_status: "routed"` is addressed to this agent, and explain that `agent_offline` and `unknown` deliveries are replies to another agent's message that this agent is receiving as a fallback, naming the `routed_agent` and `in_reply_to_text` keys as the context to use. The existing immediate-acknowledgement rule SHALL apply to routed and fallback deliveries alike.

#### Scenario: Instructions name the agent

- **WHEN** a session identified as `deploy-bot` reads the server instructions
- **THEN** the text identifies the session as `deploy-bot` and describes the `route_status` values

### Requirement: Configuration

Every new flag SHALL have a `SIGNAL_MCP_`-prefixed environment variable equivalent following the existing `signal_mcp.config` conventions, with the flag winning when both are set: `--host`, `--port`, `--auth-token`, `--default-agent`, `--route-ttl`, `--route-max-entries`. `--route-ttl` and `--route-max-entries` MUST be positive integers. The startup log SHALL report the transport, bind address, whether auth is enabled, the default agent, and the route table bounds, and MUST NOT log the token value.

#### Scenario: Env var supplies the token

- **WHEN** `SIGNAL_MCP_AUTH_TOKEN` is set and `--auth-token` is not passed
- **THEN** the HTTP server requires that token, and the startup log says auth is enabled without printing it

#### Scenario: Invalid route bound

- **WHEN** `--route-max-entries 0` is passed
- **THEN** argument parsing fails with an actionable error

### Requirement: Error Handling Standards

All error-producing operations MUST follow structured error handling:

- Errors MUST be wrapped with contextual information at each layer boundary (for example, "failed to dispatch to agent-a session <id>: connection closed")
- Sentinel errors MUST be defined for domain-specific failure modes that callers need to distinguish programmatically (an unroutable session, an unauthenticated request)
- Silent error swallowing MUST NOT occur. Every error MUST be either returned to the caller, logged with sufficient context, or explicitly handled with a documented reason for suppression
- Structured logging MUST be used for error reporting (key-value pairs such as agent id and session id, not string interpolation of secrets)

#### Scenario: One session's failure is isolated and logged

- **WHEN** delivery to one of two recipient sessions raises because its stream closed
- **THEN** the other session still receives the notification, and a warning naming the failed session's agent id is logged

### Requirement: Concurrency Safety

All concurrent operations MUST follow safe concurrency patterns:

- Context propagation MUST be used for cancellation and timeout signaling across all concurrent boundaries (the forwarder task, per-session delivery, server shutdown)
- Worker lifecycle MUST be explicitly managed. The forwarder task MUST start with the HTTP server and be cancelled and awaited on shutdown, and the daemon client MUST be closed
- Race safety MUST be ensured. The session registry and route table are mutated from the forwarder and from request handlers on the same event loop; they MUST be mutated only from that loop and MUST NOT be shared across threads without synchronization
- Concurrent tests MUST exercise a session disconnecting while a dispatch to it is in flight

#### Scenario: Shutdown stops the forwarder

- **WHEN** the HTTP server shuts down
- **THEN** the forwarder task is cancelled and awaited, the daemon connection is closed, and no delivery is attempted afterwards

## Security Requirements

<!-- Governing: ADR-0002 -->

### Authentication

All endpoints MUST require authentication by default. The HTTP channel server MUST refuse to start without `--auth-token`/`SIGNAL_MCP_AUTH_TOKEN` unless `--allow-unauthenticated` is passed explicitly, which is permitted only when the bind address is loopback. Bearer tokens SHALL be verified by fastmcp's static token verifier; a request without a valid token SHALL receive `401`.

| Endpoint | Auth | Justification |
|----------|------|---------------|
| `/mcp` (streamable HTTP, all methods) | Required | — |

There are no public endpoints.

### Rate Limiting

Rate limiting is deferred. The server serves a single operator's own agents behind a bearer token, binds to loopback by default, and every tool call is already serialized through one daemon connection; the daemon and Signal's own limits bound outbound volume. Revisit if the server is ever exposed to more than one operator.

### Security Headers

All HTTP responses MUST include the following security headers:

- `Content-Security-Policy`: `default-src 'none'`
- `X-Frame-Options`: DENY
- `X-Content-Type-Options`: nosniff
- `Referrer-Policy`: strict-origin-when-cross-origin

### Request Body Size Limits

All endpoints that accept request bodies MUST enforce size limits. Request bodies MUST be bounded to prevent unbounded memory allocation.

Default limit: 1 MB. Outbound attachments are passed to the server as local paths or `http(s)` URLs, never as inline bytes, so no endpoint needs a higher limit.

### CSRF Protection

State-changing endpoints (POST, PUT, PATCH, DELETE) MUST implement CSRF protection. Strategy: bearer token in the `Authorization` header with no cookie-based session, so a browser cannot be induced to attach credentials; the server MUST NOT set cookies.

### Redirect Validation

The server performs no HTTP redirects. Any future endpoint that redirects with a user-supplied URL MUST validate the target against an allowlist. Open redirects MUST NOT be permitted.
