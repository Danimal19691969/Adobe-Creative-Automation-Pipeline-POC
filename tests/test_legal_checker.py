"""Tests for legal pre-check callback and the post-composition disclaimer agent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from creative_pipeline.sub_agents.legal_checker.agent import (
    LegalViolation,
    _scan_prohibited_words,
    legal_precheck_callback,
)


def test_scan_finds_word_with_word_boundary():
    hits = _scan_prohibited_words(
        {"en": "The best summer offer", "es": "Refresca tu verano"},
        prohibited=["best", "guaranteed"],
    )
    assert len(hits) == 1
    assert hits[0][0] == "en"
    assert hits[0][1] == "best"


def test_scan_is_case_insensitive():
    hits = _scan_prohibited_words(
        {"en": "BEST product Ever"},
        prohibited=["best"],
    )
    assert len(hits) == 1


def test_scan_respects_word_boundaries():
    # "bestiality" should not match "best" alone
    hits = _scan_prohibited_words(
        {"en": "The bestiary is open"},
        prohibited=["best"],
    )
    assert hits == []


def test_scan_multi_word_phrase():
    hits = _scan_prohibited_words(
        {"en": "Clinically proven results"},
        prohibited=["clinically proven"],
    )
    assert len(hits) == 1


def _ctx(state: dict):
    """Minimal mock that quacks like a CallbackContext for the precheck."""
    return SimpleNamespace(state=state)


def test_callback_passes_when_clean():
    ctx = _ctx({
        "brand": {"legal": {"prohibited_words": ["best", "cure"]}},
        "brief": {"campaign_message": {"en": "Refresh your summer naturally."}},
    })
    assert legal_precheck_callback(ctx, llm_request=None) is None  # type: ignore[arg-type]


def test_callback_raises_when_violation():
    ctx = _ctx({
        "brand": {"legal": {"prohibited_words": ["best"]}},
        "brief": {"campaign_message": {"en": "The best summer ever."}},
    })
    with pytest.raises(LegalViolation, match="best"):
        legal_precheck_callback(ctx, llm_request=None)  # type: ignore[arg-type]


def test_callback_no_op_when_state_not_loaded():
    # Before brand_loader / brief_parser have run.
    ctx = _ctx({})
    assert legal_precheck_callback(ctx, llm_request=None) is None  # type: ignore[arg-type]
