"""Transport flag validation (SPEC-0002 REQ "HTTP Channel Transport").

The refusal cases are argument-parsing behavior, so they are unit tests on
``parse_args`` rather than the E2E HTTP server: ``--channel`` with an
explicitly requested sse transport (flag, ``--transport=sse``, or env var)
must exit non-zero with an actionable error, while a bare ``--channel``
keeps its pre-spec stdio coercion.
"""

import dataclasses
import os

import pytest

from signal_mcp.config import SignalConfig, config, parse_args


@pytest.fixture
def clean_env(monkeypatch):
    """Snapshot the global config and clear SIGNAL_MCP_* env vars."""
    for f in dataclasses.fields(SignalConfig):
        monkeypatch.setattr(config, f.name, getattr(config, f.name))
    for key in [k for k in os.environ if k.startswith("SIGNAL_MCP_")]:
        monkeypatch.delenv(key)
    return monkeypatch


def _refused(clean_env, argv, env=None):
    if env:
        for key, value in env.items():
            clean_env.setenv(key, value)
    with pytest.raises(SystemExit) as excinfo:
        parse_args(["--operator", "+15550000000", *argv])
    assert excinfo.value.code != 0


def test_channel_with_transport_sse_flag_refused(clean_env):
    _refused(clean_env, ["--channel", "--transport", "sse"])


def test_channel_with_transport_sse_equals_form_refused(clean_env):
    _refused(clean_env, ["--channel", "--transport=sse"])


def test_channel_with_transport_sse_env_refused(clean_env):
    _refused(clean_env, ["--channel"], env={"SIGNAL_MCP_TRANSPORT": "sse"})


def test_bare_channel_still_coerces_to_stdio(clean_env, monkeypatch):
    monkeypatch.setattr(
        "signal_mcp.main.run_channel_stdio", lambda: None, raising=False
    )
    cfg = parse_args(["--operator", "+15550000000", "--channel"])
    assert cfg.channel_mode
    assert config.transport != "sse"
