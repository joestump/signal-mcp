"""Pure A2UI v0.9 envelope builders for chat-surface rendering.

This module is **pure data-in / JSON-out** — no buffer access, no MCP, no I/O.
That purity is what makes golden-file tests able to pin the contract exactly.

The two module constants (``A2UI_VERSION`` and ``CATALOG_ID``) are the single
coordination point with the host renderer: bumping them is a coordinated
change, not a unilateral one.
"""

import json
from typing import Any


class A2UIValidationError(Exception):
    """Raised when a component list does not form a valid adjacency list."""


# Envelope version — pinned to v0.9 (the form the deployed host renderer
# consumes; not the newest spec revision, but the one that renders here).
A2UI_VERSION = "v0.9"

# The standard A2UI catalog id. Sourced from the normative A2UI-over-MCP
# transport guide (https://a2ui.org/guides/a2ui_over_mcp/) — the same value
# appears in both the initialization and per-message catalog-negotiation
# examples as ``supportedCatalogIds[0]``.
CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def envelope(*, surface_id: str, components: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the single-object v0.9 envelope.

    Returns exactly::

        {"version": "v0.9",
         "updateComponents": {
             "surfaceId": ...,
             "catalogId": ...,
             "components": [...]
         }}

    One JSON object — **not** a JSONL message stream. This is what the
    deployed host renderer's inline ``<a2ui-json>`` scanner parses.
    """
    return {
        "version": A2UI_VERSION,
        "updateComponents": {
            "surfaceId": surface_id,
            "catalogId": CATALOG_ID,
            "components": components,
        },
    }


# ---------------------------------------------------------------------------
# Component builders
#
# Each builder returns a standard-catalog component dict with a
# caller-supplied ``id``. Children are referenced **by id** (adjacency
# list), never nested inline.
# ---------------------------------------------------------------------------


def text(id: str, text_value: str) -> dict[str, Any]:
    """A Text component displaying plain text."""
    return {"component": "Text", "id": id, "text": text_value}


def heading(id: str, text_value: str) -> dict[str, Any]:
    """A Text component in the ``h2`` variant (section heading)."""
    return {"component": "Text", "id": id, "variant": "h2", "text": text_value}


def caption(id: str, text_value: str) -> dict[str, Any]:
    """A Text component in the ``caption`` variant (small, muted)."""
    return {"component": "Text", "id": id, "variant": "caption", "text": text_value}


def divider(id: str) -> dict[str, Any]:
    """A Divider component (horizontal rule between sections)."""
    return {"component": "Divider", "id": id}


def row(id: str, children: list[str]) -> dict[str, Any]:
    """A Row component laying out children horizontally."""
    return {"component": "Row", "id": id, "children": list(children)}


def column(id: str, children: list[str]) -> dict[str, Any]:
    """A Column component laying out children vertically."""
    return {"component": "Column", "id": id, "children": list(children)}


def card(id: str, child: str) -> dict[str, Any]:
    """A Card component wrapping a single child."""
    return {"component": "Card", "id": id, "child": child}


def list_(id: str, children: list[str]) -> dict[str, Any]:
    """A List component containing ordered children."""
    return {"component": "List", "id": id, "children": list(children)}


def button(
    id: str,
    label: str,
    *,
    action_name: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A Button component. ``action_name`` and ``context`` wire ``a2ui_action``.

    In v1 buttons are inert placeholders — they carry the action name and
    context so an ``a2ui_action``-capable host can wire them once the action
    tool ships, but no send path exists yet.
    """
    btn: dict[str, Any] = {"component": "Button", "id": id, "label": label}
    if action_name is not None:
        btn["actionName"] = action_name
    if context is not None:
        btn["context"] = context
    return btn


# ---------------------------------------------------------------------------
# Id allocator — deterministic ids make golden files stable
# ---------------------------------------------------------------------------


class IdAllocator:
    """Hand out deterministic, unique ids of the form ``{prefix}-{n}``.

    The first call for a prefix yields ``{prefix}-0``, the next
    ``{prefix}-1``, and so on. Determinism is what makes golden-file tests
    stable across runs.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0)
        self._counters[prefix] = n + 1
        return f"{prefix}-{n}"


# ---------------------------------------------------------------------------
# Adjacency validation
# ---------------------------------------------------------------------------


def validate_adjacency(components: list[dict[str, Any]]) -> None:
    """Validate that *components* forms a well-formed adjacency list.

    Checks:
    - Every component has a unique ``id``.
    - Exactly one root exists (a component not referenced as a child by
      any other component).
    - Every child id referenced by any component is present in the list.

    Raises :class:`A2UIValidationError` on any violation.
    """
    if not components:
        raise A2UIValidationError("component list is empty")

    # Collect all ids — check for duplicates.
    ids: set[str] = set()
    for comp in components:
        cid = comp.get("id")
        if cid is None:
            raise A2UIValidationError("component missing id field")
        if cid in ids:
            raise A2UIValidationError(f"duplicate component id: {cid!r}")
        ids.add(cid)

    # Collect all referenced child ids.
    referenced: set[str] = set()
    for comp in components:
        for key in ("children", "child"):
            val = comp.get(key)
            if val is None:
                continue
            if isinstance(val, str):
                referenced.add(val)
            elif isinstance(val, list):
                for child_id in val:
                    if isinstance(child_id, str):
                        referenced.add(child_id)

    # Every referenced child must exist.
    dangling = referenced - ids
    if dangling:
        raise A2UIValidationError(
            f"child id(s) referenced but not present: {sorted(dangling)}"
        )

    # Exactly one root: a component not referenced by any other.
    roots = ids - referenced
    if len(roots) == 0:
        raise A2UIValidationError("no root component (all components are referenced)")
    if len(roots) > 1:
        raise A2UIValidationError(
            f"multiple root components (expected 1, got {len(roots)}): {sorted(roots)}"
        )


# ---------------------------------------------------------------------------
# Convenience: build + validate in one step
# ---------------------------------------------------------------------------


def build_envelope(
    *,
    surface_id: str,
    components: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate the component list and return the completed envelope.

    Both renderers (thread and index) call this before returning, so a
    broken adjacency list fails fast rather than emitting a surface the
    host renderer cannot parse.
    """
    validate_adjacency(components)
    return envelope(surface_id=surface_id, components=components)


def dumps(envelope_dict: dict[str, Any]) -> str:
    """Serialize an envelope to a stable JSON string for golden-file tests."""
    return json.dumps(envelope_dict, sort_keys=True)
