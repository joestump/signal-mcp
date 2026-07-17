"""Tests for user-defined prompt files loaded from a prompts directory."""

import asyncio
import dataclasses
import logging
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from signal_mcp.config import config, parse_args
from signal_mcp.prompts import load_user_prompts, register_prompts

VALID_PROMPT = """\
---
name: respond-to-chelsea
description: Draft a Signal reply to Chelsea in Joe's voice.
arguments:
  - name: message
    description: The message from Chelsea to respond to
    required: true
  - name: tone
    description: Optional tone for the reply
    required: false
---
Draft a Signal reply to Chelsea.

Her message: {message}

Tone: {tone}
"""

MALFORMED_PROMPT = """\
---
name: broken
description: frontmatter never closes, so this file is malformed
Draft a reply about {topic}.
"""


def _make_mcp() -> FastMCP:
    """Create a fresh FastMCP instance with the built-in prompts registered."""
    mcp = FastMCP(name="signal-cli-test")
    register_prompts(mcp)
    return mcp


def _get_prompt_text(
    mcp: FastMCP, name: str, arguments: dict[str, str] | None = None
) -> str:
    """Render a prompt and return the text of its single user message."""
    result = asyncio.run(mcp.get_prompt(name, arguments))
    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.role == "user"
    assert isinstance(message.content, TextContent)
    return message.content.text


def test_valid_loaded_and_malformed_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One valid + one malformed file: valid registers, malformed warns."""
    (tmp_path / "respond-to-chelsea.md").write_text(VALID_PROMPT)
    (tmp_path / "broken.md").write_text(MALFORMED_PROMPT)
    mcp = _make_mcp()

    with caplog.at_level(logging.WARNING, logger="signal_mcp.prompts"):
        registered = load_user_prompts(mcp, tmp_path)

    assert registered == 1
    names = {p.name for p in asyncio.run(mcp.list_prompts())}
    assert "respond-to-chelsea" in names
    assert "broken" not in names
    # Built-ins are still there alongside the user prompt.
    assert {"signal_style", "signal_reply"} <= names
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("broken.md" in r.getMessage() for r in warnings)


def test_prompt_lists_description_and_arguments(tmp_path: Path) -> None:
    """The registered prompt exposes its description and argument schema."""
    (tmp_path / "respond-to-chelsea.md").write_text(VALID_PROMPT)
    mcp = _make_mcp()
    load_user_prompts(mcp, tmp_path)

    prompts = {p.name: p for p in asyncio.run(mcp.list_prompts())}
    prompt = prompts["respond-to-chelsea"]
    assert prompt.description == "Draft a Signal reply to Chelsea in Joe's voice."
    arguments = {a.name: a for a in prompt.arguments or []}
    assert set(arguments) == {"message", "tone"}
    assert arguments["message"].required is True
    assert arguments["message"].description == (
        "The message from Chelsea to respond to"
    )
    assert arguments["tone"].required is False


def test_render_substitutes_arguments(tmp_path: Path) -> None:
    """prompts/get substitutes provided arguments into {placeholders}."""
    (tmp_path / "respond-to-chelsea.md").write_text(VALID_PROMPT)
    mcp = _make_mcp()
    load_user_prompts(mcp, tmp_path)

    text = _get_prompt_text(
        mcp,
        "respond-to-chelsea",
        {"message": "dinner at 6?", "tone": "warm"},
    )
    assert "Her message: dinner at 6?" in text
    assert "Tone: warm" in text
    assert "{message}" not in text
    assert "{tone}" not in text


def test_missing_optional_argument_leaves_placeholder(tmp_path: Path) -> None:
    """An optional argument that is not provided leaves its placeholder as-is."""
    (tmp_path / "respond-to-chelsea.md").write_text(VALID_PROMPT)
    mcp = _make_mcp()
    load_user_prompts(mcp, tmp_path)

    text = _get_prompt_text(mcp, "respond-to-chelsea", {"message": "dinner at 6?"})
    assert "Her message: dinner at 6?" in text
    assert "{tone}" in text


def test_missing_required_argument_is_an_error(tmp_path: Path) -> None:
    """prompts/get without a required argument raises an MCP error."""
    (tmp_path / "respond-to-chelsea.md").write_text(VALID_PROMPT)
    mcp = _make_mcp()
    load_user_prompts(mcp, tmp_path)

    with pytest.raises(ValueError, match="Missing required arguments"):
        asyncio.run(mcp.get_prompt("respond-to-chelsea", {"tone": "warm"}))


def test_name_defaults_to_file_stem(tmp_path: Path) -> None:
    """A file without a frontmatter name registers under its file stem."""
    (tmp_path / "weekly-status.md").write_text(
        "---\n"
        "description: Summarize the week as a Signal note.\n"
        "---\n"
        "Summarize this week's work as a plain-text Signal note.\n"
    )
    mcp = _make_mcp()
    assert load_user_prompts(mcp, tmp_path) == 1

    names = {p.name for p in asyncio.run(mcp.list_prompts())}
    assert "weekly-status" in names
    text = _get_prompt_text(mcp, "weekly-status")
    assert text == "Summarize this week's work as a plain-text Signal note."


def test_missing_directory_is_fine(tmp_path: Path) -> None:
    """A nonexistent prompts directory means zero user prompts, no error."""
    mcp = _make_mcp()
    assert load_user_prompts(mcp, tmp_path / "does-not-exist") == 0
    names = {p.name for p in asyncio.run(mcp.list_prompts())}
    assert names == {"signal_style", "signal_reply"}


def test_non_markdown_files_are_ignored(tmp_path: Path) -> None:
    """Only *.md files are considered prompt templates."""
    (tmp_path / "notes.txt").write_text("not a prompt")
    mcp = _make_mcp()
    assert load_user_prompts(mcp, tmp_path) == 0


@pytest.mark.parametrize(
    "content",
    [
        "no frontmatter at all\n",
        "---\n---\n\n",  # empty frontmatter and empty body
        "---\nname: x\nbogus_key: y\n---\nbody\n",  # unknown top-level key
        "---\narguments:\n  - description: no name\n---\nbody\n",
        "---\narguments:\n  - name: a\n    required: maybe\n---\nbody\n",
        "---\nname: x\n---\n",  # empty body
    ],
)
def test_malformed_variants_are_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str
) -> None:
    """Each malformed shape is skipped with a warning, never a crash."""
    (tmp_path / "bad.md").write_text(content)
    mcp = _make_mcp()
    with caplog.at_level(logging.WARNING, logger="signal_mcp.prompts"):
        assert load_user_prompts(mcp, tmp_path) == 0
    assert any("bad.md" in r.getMessage() for r in caplog.records)


def test_prompts_dir_flag_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """--prompts-dir and SIGNAL_MCP_PROMPTS_DIR configure the directory."""
    snapshot = dataclasses.replace(config)
    monkeypatch.delenv("SIGNAL_MCP_PROMPTS_DIR", raising=False)
    monkeypatch.delenv("SIGNAL_MCP_TRANSPORT", raising=False)
    try:
        parse_args(["--operator", "+15550000000", "--prompts-dir", "~/my-prompts"])
        assert config.prompts_dir == Path("~/my-prompts").expanduser()

        monkeypatch.setenv("SIGNAL_MCP_PROMPTS_DIR", "/tmp/env-prompts")
        parse_args(["--operator", "+15550000000"])
        assert config.prompts_dir == Path("/tmp/env-prompts")

        monkeypatch.delenv("SIGNAL_MCP_PROMPTS_DIR")
        parse_args(["--operator", "+15550000000"])
        assert config.prompts_dir == Path("~/.config/signal-mcp/prompts").expanduser()
    finally:
        for f in dataclasses.fields(config):
            setattr(config, f.name, getattr(snapshot, f.name))
