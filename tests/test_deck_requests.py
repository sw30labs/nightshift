"""The local control API rejects ambiguous input before starting any work."""

import http.client
import json
import threading

import pytest

from nightshift.config import Settings
from nightshift.deck import DeckState, serve_deck


@pytest.fixture
def deck_http(ns_home, monkeypatch):
    calls = []
    monkeypatch.setattr(DeckState, "start_run", lambda *a, **kw: calls.append(kw) or {"ok": True})
    monkeypatch.setattr(DeckState, "start_bag", lambda *a, **kw: calls.append(kw) or {"ok": True})
    server = serve_deck(Settings(home=ns_home, mock=True, observe=False, deck_port=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address, calls
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def post(address, body, headers=None, path="/api/run"):
    client = http.client.HTTPConnection(*address, timeout=5)
    try:
        client.request("POST", path, body, headers or {"Content-Type": "application/json"})
        response = client.getresponse()
        return response.status, json.loads(response.read())
    finally:
        client.close()


@pytest.mark.parametrize("body", [b"[]", b"null", b"42", b'"hello"', b"{", b"\xff"])
def test_malformed_payload_is_json_error(deck_http, body):
    address, calls = deck_http
    code, result = post(address, body)
    assert code == 400
    assert result["error"]
    assert calls == []


@pytest.mark.parametrize("field,value", [
    ("allow_dirty", "false"), ("dry_run", "false"), ("mock", 1),
    ("brief_size", True), ("brief_size", 2.5), ("size", "2"), ("path", ["/tmp"]),
])
def test_ambiguous_field_types_do_not_start_work(deck_http, field, value):
    address, calls = deck_http
    code, result = post(address, json.dumps({"path": "/tmp/repo", field: value}))
    assert code == 400
    assert field in result["error"]
    assert calls == []


@pytest.mark.parametrize("headers,expected", [
    ({"Content-Type": "text/plain"}, 415),
    ({"Content-Type": "application/json", "Origin": "https://example.org"}, 403),
    ({"Content-Type": "application/json", "Origin": "null"}, 403),
    ({"Content-Type": "application/json", "Content-Length": "oops"}, 400),
    ({"Content-Type": "application/json", "Content-Length": "-1"}, 413),
    ({"Content-Type": "application/json", "Content-Length": "65537"}, 413),
])
def test_unsafe_request_is_rejected(deck_http, headers, expected):
    address, calls = deck_http
    code, _ = post(address, b"{}", headers)
    assert code == expected
    assert calls == []


def test_same_origin_json_can_start_work(deck_http):
    address, calls = deck_http
    code, result = post(address, b'{"path":"/tmp/repo","mock":true}', {
        "Content-Type": "application/json; charset=utf-8",
        "Origin": f"http://{address[0]}:{address[1]}",
    })
    assert code == 200 and result["ok"]
    assert len(calls) == 1


def test_deck_settings_preserve_clock_and_runtime_overrides(ns_home):
    from datetime import datetime

    now = lambda: datetime(2026, 9, 4, 2, 0)
    deadline = datetime(2026, 9, 4, 6, 0)
    settings = Settings(home=ns_home, now_fn=now, halt_deadline=deadline, deck_port=12345)
    copied = DeckState(settings)._copy_settings(mock=True, brief_size=3, allow_dirty=False, dry_run=True)
    assert copied.now_fn is now
    assert copied.halt_deadline == deadline
    assert copied.deck_port == 12345
    assert copied.mock and copied.dry_run
    assert not settings.mock and not settings.dry_run
