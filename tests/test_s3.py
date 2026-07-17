"""Tests for S3-compatible storage: config parsing and the s3 client wrapper.

All S3 API calls are stubbed with ``botocore.stub.Stubber`` — no network.
Presigning is pure local computation, so it uses a real client with fake
static credentials.
"""

import asyncio
import builtins
import logging
from dataclasses import fields, replace

import boto3
import pytest
from botocore.stub import ANY, Stubber

from signal_mcp import main as main_module
from signal_mcp import s3
from signal_mcp.config import config, parse_args

USER = ["--user-id", "+15555550100"]
SIGNAL_MCP_ENV_VARS = (
    "SIGNAL_MCP_USER_ID",
    "SIGNAL_MCP_TRANSPORT",
    "SIGNAL_MCP_RPC_HOST",
    "SIGNAL_MCP_RPC_PORT",
    "SIGNAL_MCP_TRUSTED_RECIPIENTS",
    "SIGNAL_MCP_CHANNEL",
    "SIGNAL_MCP_PREFIX",
    "SIGNAL_MCP_LOG_LEVEL",
    "SIGNAL_MCP_S3_BUCKET",
    "SIGNAL_MCP_S3_ENDPOINT_URL",
    "SIGNAL_MCP_S3_REGION",
    "SIGNAL_MCP_S3_PREFIX",
    "SIGNAL_MCP_S3_PRESIGN_TTL",
    "SIGNAL_MCP_S3_FORCE_PATH_STYLE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Keep the host environment from leaking into config parsing."""
    for var in SIGNAL_MCP_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _restore_config():
    """parse_args mutates the global config; restore it after each test."""
    snapshot = replace(config)
    yield
    for f in fields(config):
        setattr(config, f.name, getattr(snapshot, f.name))


@pytest.fixture(autouse=True)
def _reset_client():
    """Never share a cached boto3 client between tests."""
    s3.reset_client()
    yield
    s3.reset_client()


def _stubbed_client():
    """A real boto3 client with fake static credentials (never hits the network)."""
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=BotoConfig(signature_version="s3v4"),
    )


def _block_boto3(monkeypatch):
    """Make ``import boto3`` fail as if the [s3] extra were not installed."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "boto3" or name.startswith(("boto3.", "botocore")):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_s3_disabled_by_default():
    cfg = parse_args(USER)
    assert cfg.s3_bucket == ""
    assert cfg.s3_endpoint_url == ""
    assert cfg.s3_region == ""
    assert cfg.s3_prefix == "signal-mcp/"
    assert cfg.s3_presign_ttl == 3600
    assert cfg.s3_force_path_style is False
    assert s3.is_enabled() is False


def test_s3_flags():
    cfg = parse_args(
        USER
        + [
            "--s3-bucket",
            "attachments",
            "--s3-endpoint-url",
            "https://garage.example.com:3900",
            "--s3-region",
            "garage",
            "--s3-prefix",
            "sig/",
            "--s3-presign-ttl",
            "600",
        ]
    )
    assert cfg.s3_bucket == "attachments"
    assert cfg.s3_endpoint_url == "https://garage.example.com:3900"
    assert cfg.s3_region == "garage"
    assert cfg.s3_prefix == "sig/"
    assert cfg.s3_presign_ttl == 600
    assert s3.is_enabled() is True


def test_s3_env_vars(monkeypatch):
    monkeypatch.setenv("SIGNAL_MCP_S3_BUCKET", "envbucket")
    monkeypatch.setenv("SIGNAL_MCP_S3_ENDPOINT_URL", "https://minio.local:9000")
    monkeypatch.setenv("SIGNAL_MCP_S3_REGION", "us-west-2")
    monkeypatch.setenv("SIGNAL_MCP_S3_PREFIX", "env/")
    monkeypatch.setenv("SIGNAL_MCP_S3_PRESIGN_TTL", "120")
    cfg = parse_args(USER)
    assert cfg.s3_bucket == "envbucket"
    assert cfg.s3_endpoint_url == "https://minio.local:9000"
    assert cfg.s3_region == "us-west-2"
    assert cfg.s3_prefix == "env/"
    assert cfg.s3_presign_ttl == 120


def test_s3_flag_beats_env(monkeypatch):
    monkeypatch.setenv("SIGNAL_MCP_S3_BUCKET", "envbucket")
    cfg = parse_args(USER + ["--s3-bucket", "flagbucket"])
    assert cfg.s3_bucket == "flagbucket"


def test_path_style_defaults_on_with_custom_endpoint():
    cfg = parse_args(
        USER + ["--s3-bucket", "b", "--s3-endpoint-url", "https://minio.local"]
    )
    assert cfg.s3_force_path_style is True


def test_path_style_defaults_off_without_endpoint():
    cfg = parse_args(USER + ["--s3-bucket", "b"])
    assert cfg.s3_force_path_style is False


def test_path_style_explicit_off_overrides_endpoint_default():
    cfg = parse_args(
        USER
        + [
            "--s3-bucket",
            "b",
            "--s3-endpoint-url",
            "https://minio.local",
            "--no-s3-force-path-style",
        ]
    )
    assert cfg.s3_force_path_style is False


def test_path_style_explicit_on_without_endpoint():
    cfg = parse_args(USER + ["--s3-bucket", "b", "--s3-force-path-style"])
    assert cfg.s3_force_path_style is True


def test_path_style_env_overrides_endpoint_default(monkeypatch):
    monkeypatch.setenv("SIGNAL_MCP_S3_FORCE_PATH_STYLE", "false")
    cfg = parse_args(
        USER + ["--s3-bucket", "b", "--s3-endpoint-url", "https://minio.local"]
    )
    assert cfg.s3_force_path_style is False


def test_path_style_env_enables_without_endpoint(monkeypatch):
    monkeypatch.setenv("SIGNAL_MCP_S3_FORCE_PATH_STYLE", "true")
    cfg = parse_args(USER + ["--s3-bucket", "b"])
    assert cfg.s3_force_path_style is True


def test_invalid_presign_ttl_rejected(capsys):
    with pytest.raises(SystemExit):
        parse_args(USER + ["--s3-presign-ttl", "0"])
    assert "--s3-presign-ttl" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def test_get_client_wires_endpoint_region_and_path_style(monkeypatch):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    monkeypatch.setattr(config, "s3_endpoint_url", "https://garage.example.com:3900")
    monkeypatch.setattr(config, "s3_region", "garage")
    monkeypatch.setattr(config, "s3_force_path_style", True)
    client = s3.get_client()
    assert client.meta.endpoint_url == "https://garage.example.com:3900"
    assert client.meta.config.s3["addressing_style"] == "path"
    assert s3.get_client() is client  # built once, then cached


def test_get_client_defaults_to_virtual_addressing(monkeypatch):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    monkeypatch.setattr(config, "s3_region", "us-east-1")
    monkeypatch.setattr(config, "s3_force_path_style", False)
    client = s3.get_client()
    assert client.meta.config.s3["addressing_style"] == "virtual"


# ---------------------------------------------------------------------------
# Upload / presign / validate (Stubber, no network)
# ---------------------------------------------------------------------------


def test_upload_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    monkeypatch.setattr(config, "s3_prefix", "signal-mcp/")
    client = _stubbed_client()
    monkeypatch.setattr(s3, "_client", client)

    src = tmp_path / "photo.jpg"
    src.write_bytes(b"fake-jpeg-bytes")

    stubber = Stubber(client)
    stubber.add_response(
        "put_object",
        {"ETag": '"abc123"'},
        {
            "Bucket": "attachments",
            "Key": "signal-mcp/photo.jpg",
            "Body": ANY,
            "ContentType": "image/jpeg",
        },
    )
    with stubber:
        key = asyncio.run(s3.upload_file(str(src), "photo.jpg", "image/jpeg"))
        stubber.assert_no_pending_responses()

    assert key == "signal-mcp/photo.jpg"


def test_presign_uses_explicit_ttl(monkeypatch):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    monkeypatch.setattr(config, "s3_prefix", "signal-mcp/")
    monkeypatch.setattr(s3, "_client", _stubbed_client())

    url = asyncio.run(s3.presign("photo.jpg", ttl=123))

    assert "attachments" in url
    assert "signal-mcp/photo.jpg" in url
    assert "X-Amz-Expires=123" in url


def test_presign_defaults_to_configured_ttl(monkeypatch):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    monkeypatch.setattr(config, "s3_presign_ttl", 777)
    monkeypatch.setattr(s3, "_client", _stubbed_client())

    url = asyncio.run(s3.presign("photo.jpg"))

    assert "X-Amz-Expires=777" in url


def test_validate_head_bucket_ok(monkeypatch):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    client = _stubbed_client()
    monkeypatch.setattr(s3, "_client", client)

    stubber = Stubber(client)
    stubber.add_response("head_bucket", {}, {"Bucket": "attachments"})
    with stubber:
        asyncio.run(s3.validate())
        stubber.assert_no_pending_responses()


def test_validate_head_bucket_failure_is_actionable(monkeypatch):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    monkeypatch.setattr(config, "s3_endpoint_url", "https://garage.example.com")
    # A real secret in the environment must never surface in the error.
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret-value")
    client = _stubbed_client()
    monkeypatch.setattr(s3, "_client", client)

    stubber = Stubber(client)
    stubber.add_client_error(
        "head_bucket",
        service_error_code="403",
        service_message="Forbidden",
        http_status_code=403,
        expected_params={"Bucket": "attachments"},
    )
    with stubber:
        with pytest.raises(s3.S3Error) as excinfo:
            asyncio.run(s3.validate())

    msg = str(excinfo.value)
    assert "attachments" in msg  # names the bucket
    assert "https://garage.example.com" in msg  # names the endpoint
    assert "credentials" in msg  # credential hint
    assert "path-style" in msg  # path-style hint
    assert "supersecret-value" not in msg  # never leaks secrets


# ---------------------------------------------------------------------------
# Optional dependency behavior
# ---------------------------------------------------------------------------


def test_missing_boto3_names_the_extra(monkeypatch):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    _block_boto3(monkeypatch)

    with pytest.raises(s3.S3Error) as excinfo:
        s3.get_client()

    assert "pip install 'signal-mcp[s3]'" in str(excinfo.value)


def test_startup_exits_when_s3_configured_but_boto3_missing(monkeypatch, caplog):
    monkeypatch.setattr(config, "s3_bucket", "attachments")
    _block_boto3(monkeypatch)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as excinfo:
            main_module._validate_s3(config)

    assert excinfo.value.code == 1
    assert "signal-mcp[s3]" in caplog.text


def test_startup_skips_s3_when_unconfigured(monkeypatch):
    """Without a bucket the server never needs boto3 at all."""
    monkeypatch.setattr(config, "s3_bucket", "")
    _block_boto3(monkeypatch)

    main_module._validate_s3(config)  # no error, boto3 never imported
