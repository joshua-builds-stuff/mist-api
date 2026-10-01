from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES_DIR))

from mist_client import MistAPIError, MistClient  # noqa: E402


class DummyResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}
        self.content = json.dumps(payload).encode() if payload is not None else b""
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
        return self.outcomes.pop(0)

    def close(self) -> None:
        self.closed = True


def test_list_body_is_forwarded_as_json_array() -> None:
    session = DummySession([DummyResponse(200, {"added": ["ABC123", "DEF456"]})])
    client = MistClient("token", session=session)
    body = ["ABC123DEF456GHI", "JKL789MNO012PQR"]

    result = client.request_json("POST", "/orgs/org-id/inventory", json_body=body)

    assert result == {"added": ["ABC123", "DEF456"]}
    sent = session.calls[0][1]["json"]
    assert isinstance(sent, list)
    assert sent == body


def test_list_of_two_character_strings_is_not_turned_into_object() -> None:
    session = DummySession([DummyResponse(200, {})])
    client = MistClient("token", session=session)
    body = ["ab", "cd"]

    client.request("POST", "/orgs/org-id/inventory", json_body=body)

    sent = session.calls[0][1]["json"]
    assert isinstance(sent, list)
    assert sent == ["ab", "cd"]


def test_mapping_body_is_shallow_copied() -> None:
    session = DummySession([DummyResponse(200, {})])
    client = MistClient("token", session=session)
    body = {"name": "guest"}

    client.request("PUT", "/sites/site-id/wlans/wlan-id", json_body=body)
    body["name"] = "changed"
    body["extra"] = True

    sent = session.calls[0][1]["json"]
    assert sent == {"name": "guest"}
    assert sent is not body


@pytest.mark.parametrize("body", ["not-json-body", {"a", "b"}, b"bytes", 42])
def test_non_object_non_array_body_is_rejected(body: Any) -> None:
    session = DummySession()
    client = MistClient("token", session=session)

    with pytest.raises((MistAPIError, ValueError), match="object or array"):
        client.request("POST", "/orgs/org-id/inventory", json_body=body)

    assert session.calls == []
