# Button

Signal buttons are **fully rounded pills**. Use them for all primary and secondary actions.

## Classes

- `sig-btn` — required base.
- Variant (pick one): `sig-btn--primary` (Ultramarine fill, brand shadow — the main CTA), `sig-btn--secondary` (white fill, Ultramarine text — use **on top of a brand/periwinkle fill**, this is the "Get Signal" style), `sig-btn--outline` (transparent with wash border — tertiary), `sig-btn--ghost` (no border, for low-emphasis inline actions), `sig-btn--danger` (Signal end-call red).
- Size (optional): `sig-btn--sm`, `sig-btn--lg`. Default size is medium.
- `sig-btn--icon` — square-ish round icon button (call, send, etc.). Pair with an `aria-label`.

## Rules

- One primary button per view. Everything else is outline/ghost.
- Never square the corners — the pill radius (`--sig-radius-pill`) is core to the brand.
- Use `sig-btn--secondary` only over a colored surface; on white it has too little contrast.

## Examples

```html
<button class="sig-btn sig-btn--primary sig-btn--lg">Get Started</button>
<a class="sig-btn sig-btn--outline" href="/docs">Read the docs</a>
<button class="sig-btn sig-btn--danger sig-btn--icon" aria-label="End call">✕</button>
```

```jsx
<button className="sig-btn sig-btn--primary">Send message</button>
```
