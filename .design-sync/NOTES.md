# design-sync notes — Signal Design System

## What this is

The Signal MCP project has **two** design-system deliverables, both driven from
one canonical Signal token set (Ultramarine `#3b45fd`, Wash `#e3e8fe`, in-app
chat blue `#2f6bed`, near-black surfaces, Inter, pill radii):

1. **Docusaurus theme** (`website/`) — the "nice website". Tokens live in
   `website/src/css/custom.css` (`--sig-*` → Infima `--ifm-*`). Signal logos in
   `website/static/img/signal-*.svg`. Homepage hero + feature cards restyled.
   A browsable reference page at `website/src/pages/design-system.tsx`
   (route `/design-system`).

2. **claude.ai/design project** (`ds-bundle/`) — published via DesignSync so
   Claude's design agent builds Signal-branded UI. Project:
   https://claude.ai/design/p/1ababc75-09a7-4aa0-8372-3fb5b1428407

## Why this was NOT a normal design-sync run

The `/design-sync` converter expects an existing **compiled JS component
library** (`dist/`, Storybook or package build) to convert and upload. This repo
is a **Python MCP** with no such library. So the converter path (shape =
`storybook`/`package`, `package-build.mjs`, `resync.mjs`) does **not** apply.

The `ds-bundle/` layout is therefore **hand-authored** and **class-based (CSS)**:
`styles.css` + `tokens/` + HTML preview cards (`@dsCard` first line) +
`.prompt.md` per component + `README.md` conventions header. There is
intentionally **no `_ds_bundle.js`** (no React runtime — the idiom is CSS
classes) and **no `_ds_sync.json`** anchor (so a re-sync re-verifies and
re-uploads the whole bundle, which is correct for a hand-authored layout).

## Re-syncing / editing later

- Edit `ds-bundle/**`, then re-run the upload against the SAME pinned project
  (`.design-sync/config.json` → `projectId`). Because there's no anchor, upload
  everything again (finalize_plan with the same globs → write_files → re-arm the
  `_ds_needs_recompile` sentinel → reconcile deletes for anything removed).
- The conventions header lives in `ds-bundle/README.md` (source mirrored at
  `.design-sync/conventions.md`). Keep the class/token names in it TRUE —
  everything it enumerates must exist in `ds-bundle/styles.css` /
  `ds-bundle/tokens/signal.tokens.css`.
- Keep the two token sets (`website/src/css/custom.css` and
  `ds-bundle/tokens/signal.tokens.css`) in sync when values change.

## Build

- Site: `cd website && npm ci && npm run build` (Docusaurus 3.10, Node ≥20).
  npm cache had root-owned files on this machine — if `npm ci` hits EACCES, pass
  `--cache <writable-dir>`.
