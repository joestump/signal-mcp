---
status: draft
date: 2026-08-12
implements: [ADR-0001]
---

# SPEC-0001: A2UI Chat Surfaces

## Overview

signal-mcp serves rendered chat interfaces for Signal conversations to A2UI-capable MCP hosts, so a human asking about Signal messages or threads sees native chat UI — message bubbles, sender names, timestamps, attachments, and emoji reactions — instead of raw JSON or model-paraphrased markdown. Surfaces are read-only projections served as MCP resources with MIME `application/a2ui+json`, backed by a bounded in-memory conversation buffer. See ADR-0001 (A2UI Chat Surfaces over MCP Resource Templates) for the governing decision.

## Requirements

### Requirement: Conversation History Buffer

The server SHALL maintain an in-memory, per-conversation message buffer as the sole data source for A2UI surfaces.

- The conversation key SHALL be the group id for group messages, otherwise the peer's number (the counterparty for direct messages; the operator's own number for Note-to-Self traffic).
- Inbound messages and reactions SHALL be recorded from a single tap at the envelope-parse layer, so both the `receive_message` path and the channel-forwarder path populate the buffer identically. The tap MUST NOT consume from, reorder, or delay the daemon receive queue — the single-consumer invariant of the existing design is preserved.
- Outbound messages and reactions sent through this server's tools (`send`, `send_message_to_user`, `send_message_to_group`, `send_reaction_to_user`, `send_reaction_to_group`) SHALL be recorded into the same buffer after a successful daemon RPC, attributed to the server's account, so the agent's own replies appear in thread surfaces.
- The buffer SHALL be bounded at three levels, each configurable via CLI flags and environment variables following the existing `signal_mcp.config` conventions:
  - a **per-conversation message cap** (default 200), enforced FIFO — recording a message beyond the cap silently evicts that conversation's oldest message;
  - a **total conversation cap** (default 50), enforced LRU — recording traffic for a new conversation beyond the cap silently evicts the least-recently-active conversation in its entirety;
  - a **per-message stored-text cap** (default 4 KiB) — longer bodies are truncated at record time with an explicit truncation marker in the stored text.
- Eviction and truncation MUST be silent (logged at debug level, never raised as an error) and MUST NOT block, delay, or fail message delivery. There is no failure mode in which a full buffer affects the existing tool or channel paths.
- Attachments SHALL be recorded as metadata only — id, filename, content type, size — never file bytes. Sender-controlled metadata strings (filename, content type) SHALL be truncated to 256 bytes at record time. The local path and presigned URL are deliberately NOT buffered: paths may be deleted and presigned URLs expire, so surfaces render name/type/size only.
- Stored reactions SHALL follow Signal's replace-by-author semantics — one reaction per author per message, with a newer reaction from the same author replacing the older — bounding per-message reaction growth.
- The buffer is **in-memory only** and MUST NOT be persisted to disk. A server restart yields an empty buffer; the phone remains the only durable archive (per ADR-0001).

#### Scenario: Inbound message is buffered in both modes

- **WHEN** a trusted sender's message arrives while the server runs in default mode, and again while it runs in channel mode
- **THEN** in both cases the message is recorded once (never twice) in the buffer for its conversation key, and the existing `receive_message` / channel-notification behavior is unchanged

#### Scenario: Per-conversation cap evicts oldest first

- **WHEN** a conversation receives more messages than the per-conversation cap
- **THEN** the oldest buffered messages are evicted first (FIFO) and the buffer length never exceeds the cap

#### Scenario: Conversation cap evicts the least-recently-active conversation

- **WHEN** the total conversation cap is reached and a message arrives for a previously unseen conversation
- **THEN** the least-recently-active conversation is evicted in its entirety, the new conversation is recorded, and message delivery is unaffected

#### Scenario: Attachment-heavy traffic stays metadata-sized

- **WHEN** a message arrives carrying large file attachments (e.g. a multi-megabyte image)
- **THEN** the buffer grows only by the bounded metadata fields — no file bytes are read or stored, and the stored entry's size is independent of the attachment's size

#### Scenario: Oversized message is truncated, not dropped

- **WHEN** a message body longer than the stored-text cap arrives
- **THEN** the message is recorded with its text truncated at the cap and a visible truncation marker, and delivery to `receive_message` / the channel forwarder carries the full, untruncated text as today

#### Scenario: Restart clears the buffer

- **WHEN** the server process restarts and a thread surface is read before any new traffic arrives
- **THEN** the surface renders the empty state and no historical data is loaded from disk

### Requirement: Per-Instance History Divergence

The buffer is instance-local by design. Each server process SHALL maintain its own private buffer covering only envelopes observed on its own daemon connection during its own lifetime, plus sends issued through its own tools. The server MUST NOT synchronize buffers across instances or share them through external storage. Concurrent instances (e.g. several channel-mode sessions against the same daemon) will therefore hold divergent histories — different process start times, and outbound sends recorded only by the instance that issued them. This divergence is accepted per ADR-0001 and MUST be disclosed on every surface (see the scope-disclosure clauses of the thread and index resource requirements) rather than hidden; the phone remains the only complete record.

#### Scenario: Concurrent instances render independent views

- **WHEN** two server instances run concurrently against the same daemon and one of them sends a message through its own send tools
- **THEN** only the sending instance's surfaces show that outbound message, and each instance's surfaces reflect only the traffic observed during its own lifetime

### Requirement: Trusted-Sender Gating of Buffered Content

Content that the trusted-senders gate would drop MUST NOT be recorded in the buffer or rendered on any surface. When a trusted-senders allowlist is configured, messages and reactions from authors not on the allowlist MUST be excluded at record time, so A2UI surfaces can never become a side channel around the existing trust boundary.

#### Scenario: Untrusted sender never reaches a surface

- **WHEN** a trusted-senders allowlist is configured and a message arrives from an author not on it
- **THEN** the message is not recorded in the buffer, and reading any A2UI surface shows no trace of it

### Requirement: Reaction Attachment

Emoji reactions SHALL be attached to the buffered message identified by the reaction's `(target_author, target_timestamp)` pair, and rendered on that message's chat bubble.

- A reaction whose target message is not in the buffer SHALL be ignored for rendering purposes (no error, no orphan row).
- A reaction removal (`is_remove`) SHALL remove the matching emoji-by-author from the target message's rendered reactions.
- Multiple reactions on one message SHALL all render, grouped on the target bubble.

#### Scenario: Reaction renders on the correct bubble

- **WHEN** a buffered message later receives a 👍 reaction referencing its author and timestamp
- **THEN** the thread surface shows 👍 attached to that message's bubble, not as a separate message

#### Scenario: Reaction removal clears the emoji

- **WHEN** a sender withdraws a previously rendered reaction
- **THEN** the next read of the thread surface no longer shows that reaction on the target bubble

#### Scenario: Reaction to an unbuffered message is harmless

- **WHEN** a reaction arrives targeting a message that is not (or no longer) in the buffer
- **THEN** surface rendering succeeds and the reaction simply does not appear

### Requirement: Conversation Thread Resource

The server SHALL register an MCP resource template serving a chat-thread surface for one conversation, dual-registered as `signal://conversation/{id}/a2ui` and `mcp://signal/conversation/{id}/a2ui` (both routing to the same handler).

- The resource SHALL declare MIME type `application/a2ui+json` and audience annotation `["user"]`.
- `{id}` SHALL be percent-decoded before lookup and accepts either an E.164 number or a Signal group id (group ids are base64 and can contain `/` and `=`, so callers MUST percent-encode them).
- The surface SHALL render the conversation's buffered messages oldest-first (newest last), each as a chat bubble carrying: sender display name (profile/contact name when known, else the number), a human-readable timestamp, the message text, one annotation line per attachment (filename or id, content type, human-readable size), and any attached reactions.
- Messages authored by the server's own account SHALL be visually distinguished from the counterparty's (e.g. an "agent" alignment or label), so the thread reads as a two-sided chat.
- Every thread surface SHALL carry a scope disclosure: a caption stating that the view is this server instance's in-memory view since the process started, with the process start time (e.g. "This instance's view since 2026-08-12 14:02 UTC · 34 buffered messages · the phone is the complete record").
- Reading the resource for an unknown or empty conversation SHALL return a valid surface with an honest empty state (e.g. "No buffered messages for this conversation — history is in-memory and instance-local"), not a protocol error.
- Reading the resource MUST NOT mutate any state: no read receipts, no queue consumption, no sends.

#### Scenario: Thread renders as a chat surface

- **WHEN** a conversation has buffered inbound and outbound messages and an A2UI-capable host reads `signal://conversation/{id}/a2ui`
- **THEN** the host receives a valid `application/a2ui+json` payload rendering the messages as a two-sided chat thread, newest last

#### Scenario: Unknown conversation renders an empty state

- **WHEN** the host reads the thread resource for an id with no buffered messages
- **THEN** the read succeeds and the surface shows the empty-state text

#### Scenario: Scope disclosure is always present

- **WHEN** any thread surface renders, whether populated or empty
- **THEN** it includes the instance-local scope caption carrying the process start time

#### Scenario: Group id round-trips through percent-encoding

- **WHEN** the host reads the thread resource for a group whose id contains `/` or `=`, percent-encoded in the URI
- **THEN** the id is decoded, the group's thread renders, and the messages show their senders' names

### Requirement: Conversation Index Resource

The server SHALL register an MCP resource serving an index of buffered conversations, dual-registered as `signal://conversations/a2ui` and `mcp://signal/conversations/a2ui`, with MIME type `application/a2ui+json` and audience annotation `["user"]`.

- The index SHALL list each buffered conversation with: a label (group name or id for groups, sender display name or number for direct messages), a preview of the most recent message, the buffered message count, and the last-activity time, ordered most-recently-active first.
- The index SHALL carry the same instance-local scope disclosure as thread surfaces (in-memory view since process start).
- An empty buffer SHALL render an honest empty-state surface, not a protocol error.

#### Scenario: Index lists active conversations

- **WHEN** three conversations have buffered traffic and the host reads `signal://conversations/a2ui`
- **THEN** the surface lists all three, most-recently-active first, each with label, preview, count, and last-activity time

#### Scenario: Empty buffer renders an empty state

- **WHEN** the host reads the index before any traffic has been buffered
- **THEN** the read succeeds and the surface shows the empty-state text

### Requirement: A2UI Envelope Contract

Every A2UI resource payload SHALL be the single-object v0.9 envelope `{"version": "v0.9", "updateComponents": {"surfaceId": ..., "catalogId": ..., "components": [...]}}`, compatible with the inline `<a2ui-json>` message shape A2UI host renderers consume (per ADR-0001).

- `catalogId` SHALL identify the standard A2UI catalog; components MUST be limited to standard-catalog component types.
- The component list MUST form a valid adjacency list: component ids unique within the surface, exactly one root, and every referenced child id present in the list.
- All Signal-derived strings (message text, sender names, group names, filenames) MUST be emitted as literal component text values. They MUST NOT be interpreted as component structure, ids, action names, or data-binding paths — a message whose text looks like A2UI JSON renders as text.
- Payloads MUST NOT embed attachment bytes: no `data:` URIs or base64 file content in any component. Attachments render as textual descriptions; any future preview support renders by reference only — keeping payload size independent of media size.

#### Scenario: Envelope matches the golden contract

- **WHEN** a surface is rendered from a deterministic seeded buffer
- **THEN** the payload equals the checked-in golden envelope: version `v0.9`, an `updateComponents` object with the standard `catalogId`, and a well-formed component adjacency list

#### Scenario: Media never inflates the envelope

- **WHEN** a thread renders messages carrying image or file attachments
- **THEN** the payload contains only textual attachment descriptions and its byte size is independent of the attachment file sizes

#### Scenario: Hostile message text stays inert

- **WHEN** a buffered message's text contains A2UI-shaped JSON, component ids, or data-binding syntax
- **THEN** the rendered surface shows that text verbatim inside a Text component and the payload's component structure is unaffected

### Requirement: Audience Split and Existing-Surface Compatibility

A2UI surfaces are for the human; the model's programmatic surface is unchanged.

- A2UI resources SHALL carry the `audience: ["user"]` annotation; existing tools (`receive_message`, sends, reactions, `mark_read`) SHALL keep their current request/response shapes, byte-for-byte.
- Channel-mode instructions SHOULD mention the thread and index resources so an A2UI-capable host knows to read them when the user asks to see messages or threads.
- The server MUST remain fully functional against hosts with no A2UI or resource support: resources are additive, and no existing behavior may depend on them.

#### Scenario: Non-A2UI host is unaffected

- **WHEN** a host that never reads resources uses the server's tools exactly as today
- **THEN** every existing tool behaves identically to the pre-A2UI release

### Requirement: Interactive Actions

Surfaces SHALL be read-only at the A2UI layer in the first release; interaction arrives via the `a2ui_action` round-trip in a later phase.

- Thread-surface bubbles SHOULD include Button components for "reply" and "react", each carrying an action name and a context object with the conversation id, `target_author`, and `target_timestamp` — sufficient for an `a2ui_action`-capable host to wire them. Until the action tool ships, these buttons are inert placeholders.
- When the server ships an `a2ui_action` tool, it MUST dispatch exclusively to the existing send paths (`_send_message` / `_send_reaction`) and MUST enforce the trusted-recipients allowlist and group resolution exactly as the existing tools do. Action handling MUST NOT introduce a send path that bypasses `_ensure_trusted` / `_ensure_trusted_group`.

#### Scenario: Buttons carry actionable context

- **WHEN** a thread surface renders a message bubble
- **THEN** its "react" Button's action context contains the conversation id and the message's `target_author` and `target_timestamp`, matching what `send_reaction_to_user` / `send_reaction_to_group` require

#### Scenario: Actions cannot bypass the allowlist

- **WHEN** an `a2ui_action` invocation targets a recipient not on a configured trusted-recipients allowlist
- **THEN** the action is rejected with the same `UntrustedRecipientError` behavior as the equivalent direct tool call

### Requirement: Accessible Surface Content

The server controls content, not rendering, so it SHALL emit surfaces that give the host renderer what accessibility requires.

- Every Button SHALL have a human-readable text label child (never an icon-or-emoji-only control without text).
- Every attachment SHALL render a textual description (name, type, size) even when a future surface also renders a preview.
- Timestamps SHALL render in a human-readable form, not raw epoch milliseconds.

#### Scenario: Controls and attachments are textual

- **WHEN** a thread surface renders a message with an attachment and action buttons
- **THEN** each button has a text label and the attachment has a textual name/type/size line

### Requirement: Error Handling Standards

All error-producing operations MUST follow structured error handling:

- Errors MUST be wrapped with contextual information at each layer boundary (e.g., "failed to render thread surface: conversation lookup failed: ...").
- Sentinel errors MUST be defined for domain-specific failure modes that callers need to distinguish programmatically (e.g., a malformed resource URI vs. an internal render failure), following the existing `SignalError` exception hierarchy.
- Silent error swallowing MUST NOT occur — every error MUST be either raised to the MCP layer, logged with sufficient context, or explicitly handled with a documented reason for suppression. In particular, a failure to record into the history buffer MUST NOT break message delivery: it is logged and delivery proceeds (documented suppression).
- Structured, leveled logging MUST be used for error reporting via the established logger pattern.

#### Scenario: Buffer failure never blocks delivery

- **WHEN** recording a message into the history buffer raises an unexpected error
- **THEN** the error is logged and the message is still delivered to `receive_message` / the channel forwarder exactly as before

### Requirement: Concurrency Safety

All concurrent operations MUST follow safe concurrency patterns:

- The buffer is mutated from the daemon reader task and the send tools, and read by resource handlers, all on the same event loop — mutations and reads MUST stay on the event loop (no cross-thread access), and any operation that must not interleave MUST be guarded or made atomic with respect to await points.
- Rendering SHALL work from a stable snapshot of a conversation's messages, so a message arriving mid-render cannot corrupt the produced component tree.
- Worker lifecycle MUST remain explicitly managed — the new tap adds no new background tasks, and server shutdown behavior is unchanged.
- Concurrency-sensitive tests (concurrent record-and-render) MUST run in CI via the standard `make test` entry point.

#### Scenario: Render during arrival is consistent

- **WHEN** a thread surface render overlaps with new messages arriving in the same conversation
- **THEN** the rendered payload is a valid, internally consistent component tree representing some point-in-time snapshot of the buffer
