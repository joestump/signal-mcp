# Signal Design System

A privacy-first, high-contrast visual language derived from Signal's official
brand kit. Built for the **Signal MCP** project — a server that lets AI agents
send and receive Signal messages.

This is a **class-based (CSS) design system**. There is no JavaScript component
runtime to import — you compose plain HTML/JSX and apply the `.sig-*` classes
defined in `styles.css`. Raw design tokens are CSS custom properties named
`--sig-*`.

## Setup — wrap your UI in `sig-root`

Everything inherits the Signal type, color, and background from a root element
carrying the `sig-root` class. Put it on `<body>` (or the top-level app element).
Without it, text falls back to the browser default font and the theme-aware
`--sig-bg`/`--sig-text` surfaces are not applied.

```html
<body class="sig-root">
  <!-- your design -->
</body>
```

**Dark mode** is opt-in via `data-theme="dark"` on `<html>`, `sig-root`, or any
ancestor. All tokens and components respond automatically (lighter blues,
near-black surfaces). Do not hard-code light/dark colors — use the tokens.

## The styling idiom — classes + tokens

Style with the component classes below; reach for `var(--sig-*)` tokens only for
your own layout glue (spacing, one-off backgrounds). **Do not invent new class
names** — they won't resolve. If something isn't covered by a component class,
compose from tokens.

### Component classes

| Family | Base class | Key modifiers |
|---|---|---|
| Button (pill) | `sig-btn` | `--primary` `--secondary` `--outline` `--ghost` `--danger` · `--sm` `--lg` `--icon` |
| Badge / chip | `sig-badge` | `--brand` `--wash` `--success` `--warning` `--danger` `--neutral` |
| Card | `sig-card` | `--hover` · parts: `sig-card__icon` `sig-card__title` `sig-card__body` |
| Avatar | `sig-avatar` | `--sm` `--lg` `--note` |
| Field | `sig-field` | parts: `sig-field__label`, control `sig-input` |
| Composer | `sig-composer` | rounded chat input row (holds an `<input>` + `sig-btn--icon`) |
| Chat | `sig-thread` | bubbles: `sig-bubble` + `--sent` / `--received`, meta `sig-bubble__meta` |
| Alert | `sig-alert` | `--success` `--danger` |
| Type | `sig-display` `sig-h1` `sig-h2` `sig-h3` `sig-body` `sig-small` `sig-eyebrow` `sig-code` |
| Layout | `sig-stack` (vertical) · `sig-row` (horizontal wrap) |

### Token vocabulary (partial — see `tokens/signal.tokens.css` for all)

- **Brand:** `--sig-ultramarine` `#3b45fd` (primary), `--sig-ultramarine-hover`, `--sig-wash` `#e3e8fe`, `--sig-periwinkle`.
- **Chat:** `--sig-bubble-sent` `#2f6bed` (distinct from the brand blue — used only for sent bubbles), `--sig-bubble-received`, `--sig-note-lavender`.
- **Neutrals:** `--sig-ink` … `--sig-gray-900/800/700/500/400/300/200/100/50` … `--sig-white`.
- **Surfaces (theme-aware):** `--sig-bg` `--sig-surface` `--sig-border` `--sig-text` `--sig-text-strong` `--sig-text-muted`.
- **Semantic:** `--sig-success` `--sig-warning` `--sig-danger` (`#f5432c`, Signal's end-call red).
- **Radii:** `--sig-radius-xs/sm/md/lg/xl` and `--sig-radius-pill` (buttons/badges/avatars are always pill).
- **Spacing (4px base):** `--sig-space-1`…`--sig-space-8`.
- **Shadows:** `--sig-shadow-sm/md/lg` and `--sig-shadow-brand` (blue-tinted, for primary CTAs).
- **Type:** `--sig-font-sans` (Inter), `--sig-font-mono`.

## Brand rules that matter

- **Two blues, never swapped.** Ultramarine (`--sig-ultramarine`) is for logo, buttons, links, accents. Chat blue (`--sig-bubble-sent`) is *only* for outgoing message bubbles.
- **Everything rounds.** Buttons/badges/avatars/composers use the pill radius; cards and threads use `--sig-radius-lg`. Squared corners read as off-brand.
- **Bold, tight headlines.** Headings are weight 800 with negative tracking (baked into `sig-display`/`sig-h1`/`sig-h2`).
- **Generous whitespace + high contrast.** Signal leans on air and near-black ink, not decoration.

## Where the truth lives

- `styles.css` — every component class (read this before styling anything new).
- `tokens/signal.tokens.css` — the full token set, light + dark.
- `components/<group>/<Name>/<Name>.prompt.md` — per-component usage, rules, and examples.
- `guidelines/colors.html`, `guidelines/typography.html` — foundation references.

## One idiomatic snippet

```html
<body class="sig-root">
  <section class="sig-stack" style="max-width: 40rem; gap: var(--sig-space-5);">
    <span class="sig-eyebrow">Model Context Protocol</span>
    <h1 class="sig-display">Speak freely, from any agent.</h1>
    <p class="sig-body">End-to-end encrypted messaging for your AI agents.</p>
    <div class="sig-row">
      <button class="sig-btn sig-btn--primary sig-btn--lg">Get Started</button>
      <a class="sig-btn sig-btn--outline sig-btn--lg" href="/docs">Read the docs</a>
    </div>

    <div class="sig-thread" style="max-width: 24rem;">
      <div class="sig-bubble sig-bubble--received">Ping me when the deploy finishes.</div>
      <div class="sig-bubble sig-bubble--sent">Deploy succeeded ✅</div>
    </div>
  </section>
</body>
```
