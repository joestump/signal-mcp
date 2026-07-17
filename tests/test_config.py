"""Tests for account / operator parsing in the config module."""

import dataclasses

import pytest

from signal_mcp.config import config, parse_args


@pytest.fixture(autouse=True)
def _restore_config(monkeypatch: pytest.MonkeyPatch):
    """Snapshot and restore the global config around each test.

    Also clears the env vars that parse_args reads so a developer's shell
    (which may export SIGNAL_MCP_OPERATOR etc.) can't leak into assertions.
    """
    for var in ("SIGNAL_MCP_OPERATOR", "SIGNAL_MCP_ACCOUNT", "SIGNAL_MCP_TRANSPORT"):
        monkeypatch.delenv(var, raising=False)
    snapshot = dataclasses.replace(config)
    try:
        yield
    finally:
        for f in dataclasses.fields(config):
            setattr(config, f.name, getattr(snapshot, f.name))


def test_operator_required() -> None:
    """--operator (or SIGNAL_MCP_OPERATOR) is required."""
    with pytest.raises(SystemExit):
        parse_args([])


def test_account_defaults_to_operator() -> None:
    """When --account is omitted, the MCP runs as the operator (Note-to-Self)."""
    parse_args(["--operator", "+15550000001"])
    assert config.operator == "+15550000001"
    assert config.account == "+15550000001"


def test_account_distinct_from_operator() -> None:
    """--account sets the sender identity independently of --operator."""
    parse_args(["--operator", "+15550000001", "--account", "+353871760709"])
    assert config.operator == "+15550000001"
    assert config.account == "+353871760709"


def test_operator_and_account_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both values may come from the environment."""
    monkeypatch.setenv("SIGNAL_MCP_OPERATOR", "+15550000001")
    monkeypatch.setenv("SIGNAL_MCP_ACCOUNT", "+353871760709")
    parse_args([])
    assert config.operator == "+15550000001"
    assert config.account == "+353871760709"
