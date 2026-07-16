from __future__ import annotations

import hmac
import io
import sys
from hashlib import sha1, sha256
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES_DIR))

import webhook_receiver as webhook  # noqa: E402


def test_valid_v2_hmac_sha256_is_preferred() -> None:
    body = b'{"event":"device-up"}'
    signature = hmac.new(b"secret", body, sha256).hexdigest()
    headers = {
        "x-mist-signature-v2": f"sha256={signature}",
        "X-Mist-Signature": "not-considered",
    }
    assert webhook.verify_mist_signature(body, headers, "secret") == "v2"

    uppercase_headers = {"X-Mist-Signature-v2": f"SHA256={signature.upper()}"}
    assert webhook.verify_mist_signature(body, uppercase_headers, "secret") == "v2"


def test_v1_hmac_sha1_is_used_only_when_v2_is_absent() -> None:
    body = b'{"event":"device-up"}'
    signature = hmac.new(b"secret", body, sha1).hexdigest()
    assert (
        webhook.verify_mist_signature(
            body,
            {"X-Mist-Signature": f"sha1={signature}"},
            b"secret",
        )
        == "v1"
    )


def test_invalid_v2_never_downgrades_to_valid_v1() -> None:
    body = b'{"event":"device-up"}'
    valid_v1 = hmac.new(b"secret", body, sha1).hexdigest()
    with pytest.raises(webhook.SignatureError, match="v2"):
        webhook.verify_mist_signature(
            body,
            {
                "X-Mist-Signature-v2": "invalid",
                "X-Mist-Signature": valid_v1,
            },
            "secret",
        )


@pytest.mark.parametrize("headers", [{}, {"X-Mist-Signature": "wrong"}])
def test_missing_or_invalid_signature_is_rejected(headers: dict[str, str]) -> None:
    with pytest.raises(webhook.SignatureError):
        webhook.verify_mist_signature(b"{}", headers, "secret")


def test_json_body_limit_is_checked_before_stream_is_read() -> None:
    stream = io.BytesIO(b"{}")
    with pytest.raises(webhook.WebhookRequestError) as captured:
        webhook.read_request_body(
            {"Content-Type": "application/json", "Content-Length": "1001"},
            stream,
            max_body_bytes=1000,
        )
    assert captured.value.status == 413
    assert stream.tell() == 0


def test_json_body_accepts_charset_and_returns_exact_signed_bytes() -> None:
    body = b'{"events":[{"type":"up"},{"type":"down"}]}'
    raw = webhook.read_request_body(
        {
            "content-type": "application/json; charset=utf-8",
            "content-length": str(len(body)),
        },
        io.BytesIO(body),
        max_body_bytes=len(body),
    )
    assert raw == body
    payload = webhook.parse_json_body(raw)
    assert webhook.payload_event_count(payload) == 2


def test_invalid_signature_is_rejected_before_json_parsing() -> None:
    body = b"not-json"
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "X-Mist-Signature-v2": "invalid",
    }
    with pytest.raises(webhook.SignatureError):
        webhook.authenticate_and_parse(
            headers,
            io.BytesIO(body),
            secret=b"secret",
            max_body_bytes=100,
        )


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "LOCALHOST."])
def test_webhook_loopback_bind_is_allowed(host: str) -> None:
    assert webhook.validate_bind_host(host, allow_non_loopback=False) == host


def test_webhook_non_loopback_bind_requires_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="--allow-non-loopback"):
        webhook.validate_bind_host("0.0.0.0", allow_non_loopback=False)
    assert webhook.validate_bind_host("0.0.0.0", allow_non_loopback=True) == "0.0.0.0"


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({"Content-Length": "2"}, b"{}", 415),
        ({"Content-Type": "text/plain", "Content-Length": "2"}, b"{}", 415),
        ({"Content-Type": "application/json"}, b"{}", 411),
        ({"Content-Type": "application/json", "Content-Length": "nope"}, b"{}", 400),
        ({"Content-Type": "application/json", "Content-Length": "3"}, b"{}", 400),
        ({"Content-Type": "application/json", "Content-Length": "2"}, b"42", 400),
    ],
)
def test_invalid_content_metadata_or_json_is_rejected(
    headers: dict[str, str], body: bytes, status: int
) -> None:
    with pytest.raises(webhook.WebhookRequestError) as captured:
        raw = webhook.read_request_body(headers, io.BytesIO(body), max_body_bytes=100)
        webhook.parse_json_body(raw)
    assert captured.value.status == status
