from __future__ import annotations

import csv
import json
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES_DIR))

import get_site_devices_to_csv as device_export  # noqa: E402
import update_wlan_stub as wlan_update  # noqa: E402
from mist_client import MistAPIError, MistClient, normalize_base_url  # noqa: E402


class DummyResponse:
    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        *,
        headers: dict[str, str] | None = None,
        text: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = (
            (text if text is not None else json.dumps(payload)).encode()
            if payload is not None or text is not None
            else b""
        )
        self.closed = False

    def json(self) -> Any:
        return self._payload

    def close(self) -> None:
        self.closed = True


class DummySession:
    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.closed = False

    def request(self, *args: Any, **kwargs: Any) -> DummyResponse:
        self.calls.append((args, kwargs))
        if not self.outcomes:
            raise AssertionError("unexpected request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.mist.com/api/v1",
        "https://api.gc7.mist.com/api/v1/",
        "https://api.ac6.mist.com/api/v1",
        "https://api.eu.mist.com/api/v1",
    ],
)
def test_official_regional_base_urls_are_accepted(base_url: str) -> None:
    assert normalize_base_url(base_url).endswith("/api/v1")


def test_token_is_not_attached_until_base_url_and_path_are_validated() -> None:
    session = DummySession()
    with pytest.raises(ValueError, match="Custom Mist API hosts"):
        MistClient(
            "super-secret", base_url="https://evil.example/api/v1", session=session
        )
    assert session.calls == []

    client = MistClient(
        "super-secret",
        base_url="https://proxy.example/api/v1",
        allow_custom_base_url=True,
        session=session,
    )
    with pytest.raises(ValueError, match="origin-relative"):
        client.request_json("GET", "//evil.example/steal")
    assert session.calls == []


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.mist.com/api/v1",
        "https://api.mist.com.evil.example/api/v1",
        "https://user:password@api.mist.com/api/v1",
        "https://api.mist.com/other",
    ],
)
def test_unsafe_base_urls_are_rejected(base_url: str) -> None:
    with pytest.raises(ValueError):
        normalize_base_url(base_url)


def test_custom_https_url_requires_explicit_opt_in_and_receives_token_after_validation() -> (
    None
):
    response = DummyResponse(200, {"ok": True})
    session = DummySession([response])
    client = MistClient(
        "token-value",
        base_url="https://proxy.example:8443/api/v1",
        allow_custom_base_url=True,
        session=session,
    )
    assert client.request_json("GET", "/self") == {"ok": True}
    args, kwargs = session.calls[0]
    assert args == ("GET", "https://proxy.example:8443/api/v1/self")
    assert kwargs["headers"]["Authorization"] == "Token token-value"
    assert kwargs["allow_redirects"] is False


def test_get_retries_timeout_and_bounded_retry_after() -> None:
    sleeps: list[float] = []
    rate_limited = DummyResponse(429, {}, headers={"Retry-After": "9999"})
    session = DummySession(
        [
            requests.Timeout("must-not-leak"),
            rate_limited,
            DummyResponse(200, {"ok": True}),
        ]
    )
    client = MistClient(
        "token",
        session=session,
        max_retries=2,
        max_retry_delay=7,
        retry_jitter=0,
        sleep=sleeps.append,
    )

    assert client.request_json("GET", "/self") == {"ok": True}
    assert len(session.calls) == 3
    assert sleeps == [0.5, 7]
    assert rate_limited.closed


def test_put_is_not_retried_and_error_does_not_expose_body_or_exception_text() -> None:
    session = DummySession(
        [requests.Timeout("token=super-secret"), DummyResponse(200, {})]
    )
    client = MistClient(
        "super-secret", session=session, max_retries=3, sleep=lambda _: None
    )
    with pytest.raises(MistAPIError) as captured:
        client.request_json("PUT", "/sites/site/wlans/wlan", json_body={"ssid": "new"})
    assert len(session.calls) == 1
    assert "super-secret" not in str(captured.value)
    assert "token=" not in str(captured.value)

    body_session = DummySession([DummyResponse(401, text="secret diagnostic payload")])
    body_client = MistClient("token", session=body_session)
    with pytest.raises(MistAPIError) as body_error:
        body_client.request_json("GET", "/self")
    assert "secret diagnostic payload" not in str(body_error.value)


def test_pagination_requests_every_page_without_mutating_params() -> None:
    session = DummySession(
        [
            DummyResponse(200, [{"id": "1"}, {"id": "2"}]),
            DummyResponse(200, [{"id": "3"}]),
        ]
    )
    client = MistClient("token", session=session)
    params = {"type": "ap"}

    assert [
        item["id"]
        for item in client.paginate(
            "/sites/s/stats/devices", params=params, page_size=2
        )
    ] == [
        "1",
        "2",
        "3",
    ]
    assert params == {"type": "ap"}
    assert session.calls[0][1]["params"] == {"type": "ap", "limit": 2, "page": 1}
    assert session.calls[1][1]["params"] == {"type": "ap", "limit": 2, "page": 2}


class PaginatingSiteClient:
    def __init__(self, sites: list[dict[str, Any]]) -> None:
        self.sites = sites

    def paginate(self, _path: str) -> Iterator[dict[str, Any]]:
        yield from self.sites


def test_duplicate_site_names_fail_instead_of_silently_selecting_first() -> None:
    client = PaginatingSiteClient(
        [{"id": "first", "name": "HQ"}, {"id": "second", "name": " hq "}]
    )
    with pytest.raises(ValueError, match="ambiguous"):
        device_export.find_site_id(client, "org", "HQ")  # type: ignore[arg-type]


def test_csv_formula_injection_is_neutralized_and_type_status_are_exported(
    tmp_path: Path,
) -> None:
    output = tmp_path / "devices.csv"
    devices = [
        {
            "name": '=HYPERLINK("https://bad.example")',
            "hostname": " +cmd|' /C calc'!A0",
            "serial": "@SUM(1+1)",
            "model": "AP47",
            "type": "ap",
            "status": "connected",
        }
    ]
    assert device_export.atomic_write_csv(output, devices, "-site") == 1

    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["name"].startswith("'=")
    assert row["hostname"].startswith("' +")
    assert row["serial"].startswith("'@")
    assert row["site_id"] == "'-site"
    assert row["type"] == "ap"
    assert row["status"] == "connected"
    assert list(tmp_path.glob(".devices.csv.*.tmp")) == []


def test_atomic_csv_preserves_existing_file_if_iteration_fails(tmp_path: Path) -> None:
    output = tmp_path / "devices.csv"
    output.write_text("old-content\n", encoding="utf-8")

    def broken_devices() -> Iterator[dict[str, Any]]:
        yield {"name": "first"}
        raise RuntimeError("API stream failed")

    with pytest.raises(RuntimeError, match="API stream failed"):
        device_export.atomic_write_csv(output, broken_devices(), "site")
    assert output.read_text(encoding="utf-8") == "old-content\n"


class FakeWlanClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.base_url = "https://api.mist.com/api/v1"

    def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, path, json_body))
        if method == "PUT":
            return {"ssid": json_body["ssid"]}  # type: ignore[index]
        if not self.responses:
            raise AssertionError("unexpected GET")
        return self.responses.pop(0)


def test_wlan_update_uses_minimal_body_confirmation_and_private_rollback(
    tmp_path: Path,
) -> None:
    current = {"id": "wlan", "ssid": "Old", "auth": {"psk": "do-not-save"}}
    client = FakeWlanClient([current, current.copy(), {**current, "ssid": "New"}])
    rollback = tmp_path / "rollback.json"

    changed = wlan_update.apply_ssid_change(
        client,  # type: ignore[arg-type]
        site_id="site",
        wlan_id="wlan",
        desired_ssid="New",
        apply=True,
        confirmation="site/wlan",
        rollback_file=rollback,
    )

    assert changed
    put_calls = [call for call in client.calls if call[0] == "PUT"]
    assert put_calls == [("PUT", "/sites/site/wlans/wlan", {"ssid": "New"})]
    record = json.loads(rollback.read_text(encoding="utf-8"))
    assert record["before_ssid"] == "Old"
    assert record["applied_ssid"] == "New"
    assert "auth" not in record
    assert "psk" not in rollback.read_text(encoding="utf-8").casefold()
    if os.name != "nt":
        assert stat.S_IMODE(rollback.stat().st_mode) == 0o600


class RejectingPutWlanClient(FakeWlanClient):
    def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if method == "PUT":
            self.calls.append((method, path, json_body))
            raise MistAPIError("Mist API PUT request failed with HTTP 400")
        return super().request_json(method, path, json_body=json_body)


def test_failed_wlan_put_keeps_previous_rollback_record(tmp_path: Path) -> None:
    rollback = tmp_path / "rollback.json"
    previous = {"before_ssid": "Original", "applied_ssid": "Old"}
    rollback.write_text(json.dumps(previous), encoding="utf-8")
    client = RejectingPutWlanClient([{"ssid": "Old"}, {"ssid": "Old"}])

    with pytest.raises(MistAPIError, match="HTTP 400"):
        wlan_update.apply_ssid_change(
            client,  # type: ignore[arg-type]
            site_id="site",
            wlan_id="wlan",
            desired_ssid="New",
            apply=True,
            confirmation="site/wlan",
            rollback_file=rollback,
        )

    assert json.loads(rollback.read_text(encoding="utf-8")) == previous
    assert list(tmp_path.glob(".rollback.json.*.tmp")) == []


def test_wlan_update_blocks_missing_rollback_directory_before_put(
    tmp_path: Path,
) -> None:
    client = FakeWlanClient([{"ssid": "Old"}, {"ssid": "Old"}])
    with pytest.raises(ValueError, match="Rollback directory does not exist"):
        wlan_update.apply_ssid_change(
            client,  # type: ignore[arg-type]
            site_id="site",
            wlan_id="wlan",
            desired_ssid="New",
            apply=True,
            confirmation="site/wlan",
            rollback_file=tmp_path / "missing" / "rollback.json",
        )
    assert all(call[0] != "PUT" for call in client.calls)


def test_wlan_update_blocks_directory_rollback_target_before_put(
    tmp_path: Path,
) -> None:
    client = FakeWlanClient([{"ssid": "Old"}, {"ssid": "Old"}])
    rollback_dir = tmp_path / "rollback.json"
    rollback_dir.mkdir()
    with pytest.raises(ValueError, match="Rollback path is a directory"):
        wlan_update.apply_ssid_change(
            client,  # type: ignore[arg-type]
            site_id="site",
            wlan_id="wlan",
            desired_ssid="New",
            apply=True,
            confirmation="site/wlan",
            rollback_file=rollback_dir,
        )
    assert all(call[0] != "PUT" for call in client.calls)


def test_wlan_update_keeps_previous_rollback_when_verify_ssid_unchanged(
    tmp_path: Path,
) -> None:
    rollback = tmp_path / "rollback.json"
    previous = {"before_ssid": "Original", "applied_ssid": "Old"}
    rollback.write_text(json.dumps(previous), encoding="utf-8")
    client = FakeWlanClient([{"ssid": "Old"}, {"ssid": "Old"}, {"ssid": "Old"}])

    with pytest.raises(wlan_update.WlanUpdateError, match="Verification failed"):
        wlan_update.apply_ssid_change(
            client,  # type: ignore[arg-type]
            site_id="site",
            wlan_id="wlan",
            desired_ssid="New",
            apply=True,
            confirmation="site/wlan",
            rollback_file=rollback,
        )

    assert json.loads(rollback.read_text(encoding="utf-8")) == previous
    assert any(call[0] == "PUT" for call in client.calls)


class TimeoutPutWlanClient(FakeWlanClient):
    def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if method == "PUT":
            self.calls.append((method, path, json_body))
            raise MistAPIError(
                "Mist API PUT request failed after 1 attempt(s): Timeout"
            )
        return super().request_json(method, path, json_body=json_body)


def test_wlan_timeout_after_apply_saves_rollback_when_get_confirms(
    tmp_path: Path,
) -> None:
    rollback = tmp_path / "rollback.json"
    previous = {"before_ssid": "Original", "applied_ssid": "Old"}
    rollback.write_text(json.dumps(previous), encoding="utf-8")
    client = TimeoutPutWlanClient([{"ssid": "Old"}, {"ssid": "Old"}, {"ssid": "New"}])

    changed = wlan_update.apply_ssid_change(
        client,  # type: ignore[arg-type]
        site_id="site",
        wlan_id="wlan",
        desired_ssid="New",
        apply=True,
        confirmation="site/wlan",
        rollback_file=rollback,
    )

    assert changed
    record = json.loads(rollback.read_text(encoding="utf-8"))
    assert record["before_ssid"] == "Old"
    assert record["applied_ssid"] == "New"


def test_wlan_timeout_keeps_previous_rollback_when_ssid_unchanged(
    tmp_path: Path,
) -> None:
    rollback = tmp_path / "rollback.json"
    previous = {"before_ssid": "Original", "applied_ssid": "Old"}
    rollback.write_text(json.dumps(previous), encoding="utf-8")
    client = TimeoutPutWlanClient([{"ssid": "Old"}, {"ssid": "Old"}, {"ssid": "Old"}])

    with pytest.raises(MistAPIError, match="Timeout"):
        wlan_update.apply_ssid_change(
            client,  # type: ignore[arg-type]
            site_id="site",
            wlan_id="wlan",
            desired_ssid="New",
            apply=True,
            confirmation="site/wlan",
            rollback_file=rollback,
        )

    assert json.loads(rollback.read_text(encoding="utf-8")) == previous


def test_wlan_update_blocks_wrong_confirmation_before_put(tmp_path: Path) -> None:
    client = FakeWlanClient([{"ssid": "Old"}])
    with pytest.raises(wlan_update.WlanUpdateError, match="confirm-target"):
        wlan_update.apply_ssid_change(
            client,  # type: ignore[arg-type]
            site_id="site",
            wlan_id="wlan",
            desired_ssid="New",
            apply=True,
            confirmation="wrong/target",
            rollback_file=tmp_path / "rollback.json",
        )
    assert all(call[0] != "PUT" for call in client.calls)


def test_wlan_update_blocks_pre_write_race_without_record_or_put(
    tmp_path: Path,
) -> None:
    client = FakeWlanClient(
        [
            {"ssid": "Old", "enabled": True},
            {"ssid": "Old", "enabled": False},
        ]
    )
    rollback = tmp_path / "rollback.json"
    with pytest.raises(
        wlan_update.WlanUpdateError, match="changed after the initial read"
    ):
        wlan_update.apply_ssid_change(
            client,  # type: ignore[arg-type]
            site_id="site",
            wlan_id="wlan",
            desired_ssid="New",
            apply=True,
            confirmation="site/wlan",
            rollback_file=rollback,
        )
    assert not rollback.exists()
    assert all(call[0] != "PUT" for call in client.calls)


def test_wlan_rollback_refuses_to_overwrite_a_later_ssid() -> None:
    client = FakeWlanClient([{"ssid": "Someone Else"}])
    with pytest.raises(
        wlan_update.WlanUpdateError, match="current SSID no longer matches"
    ):
        wlan_update.apply_ssid_change(
            client,  # type: ignore[arg-type]
            site_id="site",
            wlan_id="wlan",
            desired_ssid="Old",
            apply=True,
            confirmation="site/wlan",
            rollback_file=None,
            expected_current_ssid="New",
        )
    assert all(call[0] != "PUT" for call in client.calls)
