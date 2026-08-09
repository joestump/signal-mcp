# Badge

Small pill labels for status and metadata (delivery state, tags, encryption).

## Classes

- `sig-badge` — required base (pill, bold, small).
- Tone (pick one): `sig-badge--brand` (Ultramarine, strongest emphasis), `sig-badge--wash` (soft brand tint), `sig-badge--success`, `sig-badge--warning`, `sig-badge--danger`, `sig-badge--neutral`.

## Rules

- Keep the text to 1–2 words. A leading emoji/glyph is fine (`✓`, `🔒`).
- Use `--success`/`--danger` for message delivery states (Delivered / Failed), `--wash` for quiet tags.

## Example

```html
<span class="sig-badge sig-badge--success">Delivered</span>
<span class="sig-badge sig-badge--brand">🔒 E2EE</span>
```
