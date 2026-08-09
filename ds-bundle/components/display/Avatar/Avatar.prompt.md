# Avatar

Circular avatar for contacts, groups, and agents. Fully round (`--sig-radius-pill`).

## Classes

- `sig-avatar` — base (default medium, wash background, Ultramarine initials).
- Size: `sig-avatar--sm`, `sig-avatar--lg`.
- `sig-avatar--note` — the lavender "Note to Self" treatment.
- Put an `<img>` inside for a photo; it cover-fits automatically. Otherwise use 1–2 initials or an emoji.

## Example

```html
<span class="sig-avatar">MJ</span>
<span class="sig-avatar sig-avatar--lg"><img src="/maya.jpg" alt="Maya" /></span>
<span class="sig-avatar sig-avatar--note">🗒️</span>
```
