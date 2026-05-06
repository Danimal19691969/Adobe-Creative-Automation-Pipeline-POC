"""Unit tests for the image_gen dispatcher logic.

Mocks the underlying tool fns so no real API calls are made.
"""

from __future__ import annotations

import pytest

from creative_pipeline.tools import image_gen


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip provider env vars before each test."""
    for key in ("PROVIDER", "IMAGE_PROVIDER", "IMAGE_MODEL",
                "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_backends(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_imagen(**kwargs):
        calls.append(("imagen", kwargs))
        return {"path": kwargs["out_path"], "latency_ms": 10, "model": kwargs["model"]}

    def fake_openai(**kwargs):
        calls.append(("openai", kwargs))
        return {"path": kwargs["out_path"], "latency_ms": 10, "model": kwargs["model"]}

    monkeypatch.setattr(image_gen, "_IMAGE_BACKENDS", {
        "google": (fake_imagen, "GOOGLE_API_KEY", "imagen-3.0-generate-002", "IMAGE_MODEL"),
        "openai": (fake_openai, "OPENAI_API_KEY", "gpt-image-1",             "IMAGE_MODEL"),
    })
    return calls


def test_default_provider_openai_dispatches_to_gpt_image_1(monkeypatch, fake_backends):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    result = image_gen.generate_hero_image("a prompt", "/tmp/out.png", "1:1")
    assert result["model"] == "gpt-image-1"
    assert fake_backends[0][0] == "openai"


def test_provider_google_defaults_image_provider_to_google(monkeypatch, fake_backends):
    monkeypatch.setenv("PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-test")
    result = image_gen.generate_hero_image("p", "/tmp/o.png")
    assert result["model"] == "imagen-3.0-generate-002"
    assert fake_backends[0][0] == "imagen"


def test_provider_anthropic_without_image_provider_raises(monkeypatch, fake_backends):
    monkeypatch.setenv("PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test")
    with pytest.raises(ValueError, match="anthropic"):
        image_gen.generate_hero_image("p", "/tmp/o.png")


def test_provider_anthropic_with_explicit_image_provider_google(monkeypatch, fake_backends):
    monkeypatch.setenv("PROVIDER", "anthropic")
    monkeypatch.setenv("IMAGE_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-test")
    result = image_gen.generate_hero_image("p", "/tmp/o.png")
    assert result["model"] == "imagen-3.0-generate-002"
    assert fake_backends[0][0] == "imagen"


def test_provider_anthropic_with_explicit_image_provider_openai(monkeypatch, fake_backends):
    monkeypatch.setenv("PROVIDER", "anthropic")
    monkeypatch.setenv("IMAGE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    result = image_gen.generate_hero_image("p", "/tmp/o.png")
    assert result["model"] == "gpt-image-1"
    assert fake_backends[0][0] == "openai"


def test_image_provider_overrides_provider(monkeypatch, fake_backends):
    monkeypatch.setenv("PROVIDER", "openai")
    monkeypatch.setenv("IMAGE_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-test")
    result = image_gen.generate_hero_image("p", "/tmp/o.png")
    assert fake_backends[0][0] == "imagen"


def test_image_model_env_overrides_default(monkeypatch, fake_backends):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("IMAGE_MODEL", "gpt-image-1-hd")
    result = image_gen.generate_hero_image("p", "/tmp/o.png")
    assert result["model"] == "gpt-image-1-hd"


def test_missing_api_key_raises(monkeypatch, fake_backends):
    # PROVIDER=openai, no OPENAI_API_KEY
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        image_gen.generate_hero_image("p", "/tmp/o.png")


def test_unknown_image_provider_raises(monkeypatch, fake_backends):
    monkeypatch.setenv("IMAGE_PROVIDER", "midjourney")
    with pytest.raises(ValueError, match="midjourney"):
        image_gen.generate_hero_image("p", "/tmp/o.png")
