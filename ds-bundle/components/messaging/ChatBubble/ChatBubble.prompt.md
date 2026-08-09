# ChatBubble & Thread

The signature Signal element. Sent messages use the in-app chat blue; received
messages are neutral. Each bubble tucks **one** corner in tight (the side it's
anchored to) for the classic speech-bubble read.

## Structure

- `sig-thread` — the scrollable message container (padded, rounded, surface background).
- `sig-bubble` + `sig-bubble--sent` — outgoing (align right, chat blue `--sig-bubble-sent`, white text).
- `sig-bubble` + `sig-bubble--received` — incoming (align left, neutral bubble, ink text).
- `sig-bubble__meta` — optional small timestamp/status line inside a bubble.
- `sig-composer` — the rounded "Signal message" input row; pair with a `sig-btn--icon` send button.

## Rules

- Sent is **chat blue** (`--sig-bubble-sent`, #2f6bed), which is intentionally distinct from the brand Ultramarine used for buttons/logo. Don't swap them.
- Emoji-only replies stay in a normal bubble (see the 👍 example).
- Keep bubbles ≤ ~82% width so the alignment stays legible.

## Example

```html
<div class="sig-thread">
  <div class="sig-bubble sig-bubble--received">What's the address?</div>
  <div class="sig-bubble sig-bubble--sent">
    118 68th Ave.
    <span class="sig-bubble__meta">Delivered</span>
  </div>
</div>
```
