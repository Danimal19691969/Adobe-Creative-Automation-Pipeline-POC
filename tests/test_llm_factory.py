"""Unit tests for the LiteLLM factory."""

from __future__ import annotations

import pytest

from creative_pipeline.tools import llm_factory


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("PROVIDER", "MODEL", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_litellm(monkeypatch):
    captured: list[dict] = []

    class FakeLiteLlm:
        def __init__(self, model: str):
            captured.append({"model": model})
            self.model = model

    monkeypatch.setattr(llm_factory, "LiteLlm", FakeLiteLlm)
    return captured


def test_default_provider_openai(monkeypatch, fake_litellm):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    llm = llm_factory.make_llm()
    assert llm.model == "openai/gpt-4o-mini"


def test_explicit_google(monkeypatch, fake_litellm):
    monkeypatch.setenv("PROVIDER", "google")
    monkeypatch.setenv("MODEL", "gemini-2.5-pro")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-test")
    llm = llm_factory.make_llm()
    assert llm.model == "gemini/gemini-2.5-pro"


def test_explicit_anthropic(monkeypatch, fake_litellm):
    monkeypatch.setenv("PROVIDER", "anthropic")
    monkeypatch.setenv("MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test")
    llm = llm_factory.make_llm()
    assert llm.model == "anthropic/claude-sonnet-4-5"


def test_unknown_provider_raises(monkeypatch, fake_litellm):
    monkeypatch.setenv("PROVIDER", "cohere")
    with pytest.raises(ValueError, match="cohere"):
        llm_factory.make_llm()


def test_missing_api_key_raises(monkeypatch, fake_litellm):
    # PROVIDER=openai (default), no OPENAI_API_KEY
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        llm_factory.make_llm()
