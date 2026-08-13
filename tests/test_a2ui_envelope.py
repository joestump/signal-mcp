"""Tests for the A2UI envelope skeleton, component builders, and adjacency validation (#61)."""

import json

import pytest

from signal_mcp.a2ui import (
    A2UIValidationError,
    A2UI_VERSION,
    CATALOG_ID,
    IdAllocator,
    button,
    build_envelope,
    caption,
    card,
    column,
    divider,
    dumps,
    envelope,
    heading,
    list_,
    row,
    text,
    validate_adjacency,
)

# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


def test_envelope_shape():
    """The envelope matches the v0.9 single-object form."""
    env = envelope(surface_id="test-surface", components=[])
    assert env == {
        "version": A2UI_VERSION,
        "updateComponents": {
            "surfaceId": "test-surface",
            "catalogId": CATALOG_ID,
            "components": [],
        },
    }


def test_envelope_version_is_v0_9():
    assert A2UI_VERSION == "v0.9"


def test_catalog_id_is_standard():
    """Catalog id is sourced from the A2UI-over-MCP transport guide."""
    assert CATALOG_ID == (
        "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
    )


def test_envelope_golden():
    """Golden test: the exact envelope skeleton for a known component list."""
    components = [
        card("root", "col"),
        {"component": "Column", "id": "col", "children": ["title", "body"]},
        heading("title", "Hello"),
        text("body", "World"),
    ]
    env = envelope(surface_id="golden", components=components)
    golden = {
        "version": "v0.9",
        "updateComponents": {
            "surfaceId": "golden",
            "catalogId": "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json",
            "components": [
                {"component": "Card", "id": "root", "child": "col"},
                {
                    "component": "Column",
                    "id": "col",
                    "children": ["title", "body"],
                },
                {"component": "Text", "id": "title", "variant": "h2", "text": "Hello"},
                {"component": "Text", "id": "body", "text": "World"},
            ],
        },
    }
    assert json.loads(dumps(env)) == json.loads(json.dumps(golden, sort_keys=True))


# ---------------------------------------------------------------------------
# Component builders
# ---------------------------------------------------------------------------


def test_text_builder():
    assert text("t1", "hello") == {"component": "Text", "id": "t1", "text": "hello"}


def test_heading_builder():
    assert heading("h1", "Title") == {
        "component": "Text",
        "id": "h1",
        "variant": "h2",
        "text": "Title",
    }


def test_caption_builder():
    assert caption("c1", "small") == {
        "component": "Text",
        "id": "c1",
        "variant": "caption",
        "text": "small",
    }


def test_divider_builder():
    assert divider("d1") == {"component": "Divider", "id": "d1"}


def test_row_builder():
    assert row("r1", ["a", "b"]) == {
        "component": "Row",
        "id": "r1",
        "children": ["a", "b"],
    }


def test_column_builder():
    assert column("col1", ["x", "y"]) == {
        "component": "Column",
        "id": "col1",
        "children": ["x", "y"],
    }


def test_card_builder():
    assert card("card1", "child1") == {
        "component": "Card",
        "id": "card1",
        "child": "child1",
    }


def test_list_builder():
    assert list_("l1", ["a", "b"]) == {
        "component": "List",
        "id": "l1",
        "children": ["a", "b"],
    }


def test_button_simple():
    assert button("b1", "Reply") == {
        "component": "Button",
        "id": "b1",
        "label": "Reply",
    }


def test_button_with_action():
    btn = button(
        "b1",
        "React",
        action_name="react",
        context={"conversation_id": "+1234", "target_author": "+5678"},
    )
    assert btn == {
        "component": "Button",
        "id": "b1",
        "label": "React",
        "actionName": "react",
        "context": {"conversation_id": "+1234", "target_author": "+5678"},
    }


def test_row_copies_children():
    """Row should copy its children list so later mutation is safe."""
    children = ["a"]
    r = row("r1", children)
    children.append("b")
    assert r["children"] == ["a"]


def test_list_copies_children():
    """List should copy its children list so later mutation is safe."""
    children = ["a"]
    lst = list_("l1", children)
    children.append("b")
    assert lst["children"] == ["a"]


# ---------------------------------------------------------------------------
# IdAllocator
# ---------------------------------------------------------------------------


def test_id_allocator_deterministic():
    alloc = IdAllocator()
    assert alloc.next("msg") == "msg-0"
    assert alloc.next("msg") == "msg-1"
    assert alloc.next("row") == "row-0"
    assert alloc.next("msg") == "msg-2"


def test_id_allocator_independent_instances():
    a1 = IdAllocator()
    a2 = IdAllocator()
    assert a1.next("x") == "x-0"
    assert a2.next("x") == "x-0"  # independent


# ---------------------------------------------------------------------------
# validate_adjacency — passing case
# ---------------------------------------------------------------------------


def test_validate_adjacency_passing():
    components = [
        card("root", "col"),
        column("col", ["a", "b"]),
        text("a", "hello"),
        text("b", "world"),
    ]
    validate_adjacency(components)  # no exception


def test_build_envelope_validates_and_returns():
    components = [
        card("root", "child"),
        text("child", "hello"),
    ]
    env = build_envelope(surface_id="test", components=components)
    assert env["version"] == "v0.9"
    assert env["updateComponents"]["components"] == components


# ---------------------------------------------------------------------------
# validate_adjacency — failure modes
# ---------------------------------------------------------------------------


def test_validate_adjacency_duplicate_id():
    components = [
        card("root", "x"),
        text("x", "hello"),
        text("x", "world"),  # duplicate id
    ]
    with pytest.raises(A2UIValidationError, match="duplicate"):
        validate_adjacency(components)


def test_validate_adjacency_dangling_child():
    components = [
        card("root", "missing"),  # references non-existent child
    ]
    with pytest.raises(A2UIValidationError, match="not present"):
        validate_adjacency(components)


def test_validate_adjacency_zero_roots():
    """When every component is referenced, there is no root."""
    components = [
        {"component": "Card", "id": "a", "child": "b"},
        {"component": "Card", "id": "b", "child": "a"},  # circular, no root
    ]
    with pytest.raises(A2UIValidationError, match="no root"):
        validate_adjacency(components)


def test_validate_adjacency_two_roots():
    components = [
        text("a", "hello"),
        text("b", "world"),  # neither is referenced
    ]
    with pytest.raises(A2UIValidationError, match="multiple root"):
        validate_adjacency(components)


def test_validate_adjacency_empty_list():
    with pytest.raises(A2UIValidationError, match="empty"):
        validate_adjacency([])


def test_validate_adjacency_missing_id():
    components = [
        {"component": "Text", "text": "no id"},
    ]
    with pytest.raises(A2UIValidationError, match="missing id"):
        validate_adjacency(components)
