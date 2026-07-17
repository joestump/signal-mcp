# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands
- Run: `uv run signal-mcp --operator OPERATOR_NUMBER [--account ACCOUNT_NUMBER] [--transport {sse|stdio}]`
  - `--operator` = the human the agent serves (who it messages / listens to).
  - `--account` = the number the MCP runs as (the daemon's `-a`); defaults to `--operator` (Note to Self).
- Run with channel mode: `uv run signal-mcp --operator OPERATOR_NUMBER --channel [--prefix PREFIX]`
- Test: `uv run --extra test pytest tests/`
- Lint: `uv run ruff check .`
- Type check: `uv run mypy .`
- Format code: `uv run ruff format .`

## Code Style Guidelines
- **Imports**: Standard library first, then third-party, then local. Group imports by type.
- **Formatting**: Use ruff formatter (Black-compatible).
- **Types**: Use strict type annotations. Define custom types for complex structures.
- **Naming**:
  - Functions/variables: snake_case
  - Classes: PascalCase
  - Constants: UPPER_CASE
- **Error Handling**: Use custom exception classes. Log errors before raising or returning.
- **Logging**: Use the established logger pattern with appropriate log levels.
- **Async**: Use asyncio for all I/O operations.
- **Security**: Always use shlex.quote for shell command arguments.