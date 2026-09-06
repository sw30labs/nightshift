from __future__ import annotations

import json

from nightshift.llm import (
    DEFAULT_MAX_TOKENS,
    OUTPUT_RESERVE_TOKENS,
    REASONING_EFFORT,
    WRITER_MAX_TOKENS,
    WRITER_MAX_TOKENS_ON_TRUNCATE,
    OpenAICompatClient,
    Writer,
    thinking_request_fields,
)
from nightshift.models import Brief, Upgrade


def _brief() -> Brief:
    return Brief.freeze(
        [
            Upgrade(id=1, title="a", check_command="true", paths=["widget.py"]),
            Upgrade(id=2, title="b", check_command="true", paths=["widget.py"]),
        ]
    )


def test_thinking_request_fields_are_max_effort_with_output_reserve():
    fields = thinking_request_fields(DEFAULT_MAX_TOKENS)
    assert fields["reasoning_effort"] == "max"
    assert REASONING_EFFORT == "max"
    assert fields["thinking"] is True
    kwargs = fields["chat_template_kwargs"]
    assert kwargs["thinking"] is True
    assert kwargs["enable_thinking"] is True
    assert kwargs["reasoning_effort"] == "max"
    reserved = DEFAULT_MAX_TOKENS - OUTPUT_RESERVE_TOKENS
    assert fields["thinking_budget"] == reserved
    assert fields["thinking_token_budget"] == reserved
    assert fields["thinking_budget"] + OUTPUT_RESERVE_TOKENS == DEFAULT_MAX_TOKENS


def test_small_max_tokens_omits_thinking_budget():
    fields = thinking_request_fields(4096)
    assert "thinking_budget" not in fields
    assert "thinking_token_budget" not in fields
    assert fields["reasoning_effort"] == "max"


def test_chat_payload_sets_max_thinking_and_reserves_output(monkeypatch):
    captured: dict = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": '{"ok": true}'},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ).encode()

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("nightshift.llm.urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatClient("http://localhost:1/v1", "ds4")
    text = client.chat([{"role": "user", "content": "hi"}])
    assert json.loads(text)["ok"] is True
    body = captured["body"]
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS
    assert body["reasoning_effort"] == "max"
    assert body["thinking"] is True
    assert body["chat_template_kwargs"]["reasoning_effort"] == "max"
    assert body["chat_template_kwargs"]["thinking"] is True
    assert body["chat_template_kwargs"]["enable_thinking"] is True
    assert body["thinking_budget"] == DEFAULT_MAX_TOKENS - OUTPUT_RESERVE_TOKENS
    assert body["thinking_token_budget"] == body["thinking_budget"]


def test_writer_raises_token_budget_after_length_truncate(fixture_repo):
    class Trunc:
        mock = False

        def __init__(self) -> None:
            self.seen: list[int] = []
            self.last_finish_reason = "length"

        def chat(self, messages, **kwargs):
            self.seen.append(int(kwargs.get("max_tokens") or 0))
            return "not json"

    client = Trunc()
    result = Writer(client, fixture_repo).apply_job("edit widget", _brief(), "")
    assert result.written == []
    assert client.seen[0] == WRITER_MAX_TOKENS
    assert client.seen[1] == WRITER_MAX_TOKENS_ON_TRUNCATE
    assert WRITER_MAX_TOKENS_ON_TRUNCATE > WRITER_MAX_TOKENS
    assert WRITER_MAX_TOKENS >= OUTPUT_RESERVE_TOKENS * 2
