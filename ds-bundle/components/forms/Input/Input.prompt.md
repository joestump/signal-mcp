# Input & Field

Text inputs are rounded (`--sig-radius-md`) with a subtle surface fill and an
Ultramarine focus ring.

## Structure

- `sig-field` — wraps a label + control in a vertical stack.
- `sig-field__label` — the label.
- `sig-input` — the text control (works for text/tel/email/etc.).
- `sig-composer` — a fully-rounded pill row for the "Signal message" chat composer; put a bare `<input>` and a `sig-btn--icon` send button inside.

## Rules

- Focus state is `--sig-ultramarine` border + a `--sig-wash` glow. Don't remove the focus ring.
- Use `sig-composer` (pill) only for message entry; use `sig-input` (rounded rect) for form fields.

## Example

```html
<div class="sig-field">
  <label class="sig-field__label" for="phone">Phone number</label>
  <input class="sig-input" id="phone" type="tel" placeholder="+1 555 010 1234" />
</div>
```
