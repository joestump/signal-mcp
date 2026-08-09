# Card

The content container for features, docs highlights, and grouped info. Rounded
(`--sig-radius-lg`), 1px border, soft shadow, lifts on hover.

## Structure

- `sig-card` — the container. Add `sig-card--hover` for the lift-on-hover interaction (use for clickable/linked cards).
- `sig-card__icon` — a rounded wash-filled chip for an emoji or icon (optional, sits at the top).
- `sig-card__title` — heading.
- `sig-card__body` — muted body copy.

## Rules

- Lay cards out in a responsive grid (`repeat(auto-fit, minmax(240px, 1fr))`).
- The icon chip uses the `--sig-wash` background — do not recolor it per-card; consistency is the point.

## Example

```html
<div class="sig-card sig-card--hover">
  <div class="sig-card__icon">🔒</div>
  <h3 class="sig-card__title">End-to-end encrypted</h3>
  <p class="sig-card__body">We can't read your messages. No one else can either.</p>
</div>
```
