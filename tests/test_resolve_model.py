from __future__ import annotations

from nightshift.config import Settings
from nightshift.llm import resolve_model_id
from nightshift.runner import probe_brains


def test_resolve_auto_picks_first():
    assert resolve_model_id("auto", ["vision-a", "vision-b"]) == "vision-a"
    assert resolve_model_id("*", ["only"]) == "only"
    assert resolve_model_id("", ["served"]) == "served"
    assert resolve_model_id("  AUTO  ", ["served"]) == "served"


def test_resolve_exact_match_keeps():
    ids = ["deepseek-v4-flash", "deepseek-v4-flash-0731-vision"]
    assert resolve_model_id("deepseek-v4-flash", ids) == "deepseek-v4-flash"
    assert (
        resolve_model_id("deepseek-v4-flash-0731-vision", ids)
        == "deepseek-v4-flash-0731-vision"
    )


def test_resolve_mismatch_picks_first():
    ids = ["deepseek-v4-flash-0731-vision"]
    assert resolve_model_id("deepseek-v4-flash", ids) == "deepseek-v4-flash-0731-vision"


def test_resolve_empty_ids_keeps_configured():
    assert resolve_model_id("deepseek-v4-flash", []) == "deepseek-v4-flash"
    assert resolve_model_id("auto", []) == "auto"
    assert resolve_model_id("  kept  ", []) == "kept"


def test_probe_brains_mutates_writer_on_mismatch(monkeypatch, ns_home):
    settings = Settings(
        mock=False,
        observe=False,
        home=ns_home,
        writer_model="deepseek-v4-flash",
        critic_model="GLM-5.3-Flash-MLX-8bit",
        writer_base_url="http://writer.test/v1",
        critic_base_url="http://critic.test/v1",
    )
    bodies = {
        "http://writer.test/v1": {
            "data": [{"id": "deepseek-v4-flash-0731-vision"}]
        },
        "http://critic.test/v1": {
            "data": [{"id": "GLM-5.3-Flash-MLX-8bit"}]
        },
    }

    def fake_probe(base_url: str, api_key: str, *, timeout: float = 5):
        return bodies[base_url]

    logs: list[str] = []
    monkeypatch.setattr("nightshift.runner.probe_models", fake_probe)
    monkeypatch.setattr("nightshift.observe.log", logs.append)

    probe_brains(settings)
    assert settings.writer_model == "deepseek-v4-flash-0731-vision"
    assert settings.critic_model == "GLM-5.3-Flash-MLX-8bit"
    assert any(
        "writer model 'deepseek-v4-flash' → 'deepseek-v4-flash-0731-vision'" in line
        for line in logs
    )


def test_probe_brains_auto_writer(monkeypatch, ns_home):
    settings = Settings(
        mock=False,
        observe=False,
        home=ns_home,
        writer_model="auto",
        critic_model="auto",
        writer_base_url="http://writer.test/v1",
        critic_base_url="http://critic.test/v1",
    )

    def fake_probe(base_url: str, api_key: str, *, timeout: float = 5):
        if "writer" in base_url:
            return {"data": [{"id": "served-writer"}, {"id": "other"}]}
        return {"data": [{"id": "served-critic"}]}

    monkeypatch.setattr("nightshift.runner.probe_models", fake_probe)
    monkeypatch.setattr("nightshift.observe.log", lambda *_a, **_k: None)

    probe_brains(settings)
    assert settings.writer_model == "served-writer"
    assert settings.critic_model == "served-critic"
