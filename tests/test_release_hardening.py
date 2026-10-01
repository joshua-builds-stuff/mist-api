from __future__ import annotations

import argparse
import hmac
import io
import json
import os
import socket
import stat
import sys
import threading
from email.message import Message
from hashlib import sha256
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import mist_client as client_module  # noqa: E402
import update_wlan_stub as wlan  # noqa: E402
import webhook_receiver as webhook  # noqa: E402
from scripts import query_spec as query, spec_cache  # noqa: E402


def test_shared_schema_dag_is_memoized() -> None:
    schemas = {"s0": {"properties": {"ssid": {"type": "string"}}}}
    for index in range(1, 25):
        schemas[f"s{index}"] = {
            "allOf": [{"$ref": f"#/components/schemas/s{index - 1}"}] * 2
        }
    match = query._find_schema_property(
        {"components": {"schemas": schemas}}, schemas["s24"], "ssid"
    )
    assert match.schema == {"type": "string"}


@pytest.mark.parametrize("kind", ["property", "expansion", "ref"])
def test_deep_schema_traversal_fails_safely(kind: str) -> None:
    node = {"properties": {"ssid": {"type": "string"}}}
    for _ in range(100):
        node = {"allOf": [node]}
    with pytest.raises(query.QueryError, match="resource limits"):
        if kind == "property":
            query._find_schema_property({}, node, "ssid")
        elif kind == "expansion":
            query._expand_schema({}, node, 0, budget=query._expansion_budget())
        else:
            schemas = {
                f"s{i}": {"$ref": f"#/components/schemas/s{i + 1}"} for i in range(100)
            }
            query._resolve_object_ref(
                {"components": {"schemas": schemas}},
                {"$ref": "#/components/schemas/s0"},
            )


@pytest.mark.parametrize(
    "schema",
    [
        {
            "allOf": [
                {"properties": {"ssid": {"type": "string"}}},
                {"required": ["ssid"]},
            ]
        },
        {"$ref": "#/components/schemas/base", "required": ["ssid"]},
    ],
)
def test_required_constraint_without_property_definition(schema) -> None:
    spec = {
        "components": {
            "schemas": {"base": {"properties": {"ssid": {"type": "string"}}}}
        }
    }
    match = query._find_schema_property(spec, schema, "ssid")
    assert match.required is True
    assert match.schema == {"type": "string"}


def test_spec_read_is_bounded_after_stat() -> None:
    stream = io.BytesIO(b"x" * 100)
    with (
        mock.patch.object(Path, "stat", return_value=mock.Mock(st_size=1)),
        mock.patch.object(Path, "open", return_value=stream),
    ):
        with pytest.raises(spec_cache.SpecValidationError, match="size limit"):
            spec_cache.read_json_document(Path("growing.json"), max_bytes=16)
        assert stream.closed


def test_rollback_read_is_bounded_after_stat() -> None:
    stream = io.BytesIO(b"x" * (wlan.MAX_ROLLBACK_RECORD_BYTES + 2))
    with (
        mock.patch.object(Path, "stat", return_value=mock.Mock(st_size=1)),
        mock.patch.object(Path, "open", return_value=stream),
        mock.patch.object(stream, "read", wraps=stream.read) as read,
    ):
        with pytest.raises(wlan.WlanUpdateError, match="16 KiB safety limit"):
            wlan.load_rollback_record(Path("growing.json"))
        read.assert_called_once_with(wlan.MAX_ROLLBACK_RECORD_BYTES + 1)
        assert stream.closed


@pytest.mark.parametrize("error", [RecursionError, ValueError])
def test_rollback_parser_limit_errors_are_normalized(tmp_path, error) -> None:
    record = tmp_path / "rollback.json"
    record.write_text("{}", encoding="utf-8")
    with mock.patch.object(json, "loads", side_effect=error("private body")):
        with pytest.raises(wlan.WlanUpdateError, match="valid JSON") as captured:
            wlan.load_rollback_record(record)
    assert "private body" not in str(captured.value)


def test_parser_resource_errors_are_normalized() -> None:
    with mock.patch.object(json, "loads", side_effect=RecursionError("private body")):
        with pytest.raises(webhook.WebhookRequestError, match="valid UTF-8 JSON"):
            webhook.parse_json_body(b"{}")
    with mock.patch.object(json, "loads", side_effect=ValueError("private body")):
        with pytest.raises(webhook.WebhookRequestError, match="valid UTF-8 JSON"):
            webhook.parse_json_body(b"{}")


@pytest.mark.parametrize(
    "name",
    ["Content-Length", "Content-Type", "X-Mist-Signature", "X-Mist-Signature-v2"],
)
def test_duplicate_headers_are_rejected(name: str) -> None:
    headers = Message()
    headers[name] = "first"
    headers[name] = "second"
    with pytest.raises(webhook.WebhookRequestError, match="Duplicate"):
        webhook._header(headers, name)


@pytest.mark.parametrize("length", ["+2", "-2", "2_0", "٢", "2\n"])
def test_content_length_is_strict_ascii(length: str) -> None:
    with pytest.raises(webhook.WebhookRequestError) as captured:
        webhook.read_request_body(
            {"Content-Type": "application/json", "Content-Length": length},
            io.BytesIO(b"{}"),
            max_body_bytes=100,
        )
    assert captured.value.status == 400


class ObservedServer(webhook.MistWebhookServer):
    def finish_request(self, request, client_address):
        self.admitted.set()
        super().finish_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.finished.set()


@pytest.fixture
def receiver():
    server = ObservedServer(
        ("0.0.0.0", 0),
        b"test-secret",
        1024,
        max_connections=1,
        request_timeout_seconds=0.15,
        connection_deadline_seconds=0.4,
    )
    server.admitted = threading.Event()
    server.finished = threading.Event()
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def connect(server):
    return socket.create_connection(("127.0.0.1", server.server_port), timeout=2)


def test_receiver_bounds_idle_connections_and_releases_slots(receiver) -> None:
    with connect(receiver) as stalled:
        assert receiver.admitted.wait(2)
        with connect(receiver) as rejected:
            assert rejected.recv(1) == b""
        assert receiver.finished.wait(2)
        assert stalled.recv(1) == b""
    receiver.finished.clear()
    with connect(receiver) as connection:
        connection.sendall(
            b"POST /mist/webhook HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 99\r\n\r\n{}"
        )
        assert b"400" in connection.recv(4096)
    assert receiver.finished.wait(2)


def test_connection_deadline_stops_trickling_headers(receiver) -> None:
    with connect(receiver) as connection:
        assert receiver.admitted.wait(2)

        def trickle():
            while not receiver.finished.wait(0.03):
                try:
                    connection.sendall(b"P")
                except OSError:
                    return

        writer = threading.Thread(target=trickle, daemon=True)
        writer.start()
        assert receiver.finished.wait(2)
        writer.join(timeout=2)
        assert not writer.is_alive()


@pytest.mark.parametrize(
    "extra,status",
    [
        (b"Transfer-Encoding: chunked\r\n", 400),
        (b"X-Mist-Signature-v2: bad\r\nX-Mist-Signature-v2: bad\r\n", 400),
        (b"", 200),
    ],
)
def test_receiver_handler_framing_and_valid_signature(receiver, extra, status) -> None:
    body = b"{}"
    signature = hmac.new(b"test-secret", body, sha256).hexdigest().encode()
    with connect(receiver) as connection:
        signature_header = (
            b""
            if b"X-Mist" in extra
            else b"X-Mist-Signature-v2: " + signature + b"\r\n"
        )
        connection.sendall(
            b"POST /mist/webhook HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
            + signature_header
            + extra
            + b"\r\n"
            + body
        )
        assert str(status).encode() in connection.recv(4096)
    assert receiver.finished.wait(2)


@pytest.mark.parametrize("status", [None, 302, 408, 500, 502, 504])
def test_write_error_outcome_is_structured(status) -> None:
    assert client_module.MistAPIError(
        "arbitrary message", status_code=status
    ).outcome_indeterminate
    assert not client_module.MistAPIError(
        "timeout misleading text", status_code=400
    ).outcome_indeterminate


class FailingWriteClient:
    base_url = client_module.DEFAULT_BASE_URL

    def __init__(self, *, verification_fails=False):
        self.ssid = "Old"
        self.calls = []
        self.verification_fails = verification_fails

    def request_json(self, method, path, *, json_body=None):
        self.calls.append(method)
        if method == "PUT":
            self.ssid = json_body["ssid"]
            raise client_module.MistAPIError("HTTP server failure", status_code=502)
        if self.ssid == "New" and self.verification_fails:
            raise client_module.MistAPIError("GET timeout")
        return {"ssid": self.ssid}


def test_committed_write_then_server_failure_is_verified(tmp_path) -> None:
    client = FailingWriteClient()
    record = tmp_path / "rollback.json"
    assert wlan.apply_ssid_change(
        client,
        site_id="site",
        wlan_id="wlan",
        desired_ssid="New",
        apply=True,
        confirmation="site/wlan",
        rollback_file=record,
    )
    assert client.calls == ["GET", "GET", "PUT", "GET"]
    assert json.loads(record.read_text())["before_ssid"] == "Old"


def test_unknown_write_saves_pending_recovery_without_replacing_confirmed(
    tmp_path,
) -> None:
    client = FailingWriteClient(verification_fails=True)
    record = tmp_path / "rollback.json"
    record.write_text("previous confirmed record")
    with pytest.raises(wlan.WlanUpdateError, match="outcome is unknown"):
        wlan.apply_ssid_change(
            client,
            site_id="site",
            wlan_id="wlan",
            desired_ssid="New",
            apply=True,
            confirmation="site/wlan",
            rollback_file=record,
        )
    assert record.read_text() == "previous confirmed record"
    pending = record.with_name("rollback.json.pending.json")
    data = json.loads(pending.read_text())
    assert data["outcome"] == "indeterminate"
    assert data["before_ssid"] == "Old"
    assert data["applied_ssid"] == "New"
    if os.name != "nt":
        assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    with pytest.raises(wlan.WlanUpdateError, match="unexpected schema"):
        wlan.load_rollback_record(pending)


def test_invalid_port_environment_has_friendly_cli_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PORT", "not-a-number")
    monkeypatch.setattr(sys, "argv", ["webhook_receiver.py"])
    with pytest.raises(SystemExit) as captured:
        webhook.parse_args()
    assert captured.value.code == 2
    assert "invalid int value" in capsys.readouterr().err


def test_schema_visit_budget_is_enforced_and_reset() -> None:
    schema = {"allOf": [{"properties": {"ssid": {"type": "string"}}}] * 20}
    with mock.patch.object(query, "MAX_TRAVERSAL_NODES", 5):
        with pytest.raises(query.QueryError, match="resource limits"):
            query._find_schema_property({}, schema, "ssid")
    assert query._find_schema_property({}, schema, "ssid").schema == {"type": "string"}


def test_endpoint_search_retains_best_results_and_counts_matches() -> None:
    spec = {
        "paths": {
            f"/api/v1/wlans/{i}": {"get": {"summary": "list WLANs"}} for i in range(100)
        }
    }
    args = argparse.Namespace(term="wlans", limit=3)
    output = query.cmd_find(spec, args).value
    assert len(output.splitlines()) == 4
    assert "97 more" in output
    assert "/wlans/0" in output
    assert "/wlans/1 " in output
    assert "/wlans/2 " in output


def test_secret_scanner_exempts_only_exact_fixture_and_omits_values(tmp_path) -> None:
    from detect_secrets.settings import default_settings
    from tools.check_secrets import scan_content

    dummy = '"https://' + "user" + ":" + "password" + '@api.mist.com/api/v1",'
    with default_settings():
        assert (
            scan_content(
                "tests/test_examples.py", dummy.encode(), tmp_path / "source.txt"
            )
            == []
        )
        findings = scan_content("other.py", dummy.encode(), tmp_path / "source.txt")
    assert findings == ["other.py:1: Basic Auth Credentials"]
    assert not any("password" in finding for finding in findings)
