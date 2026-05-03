"""Tests for the LLM secondary-check module.

The verify_listing() function makes a real LLM call so it can't be
covered by unit tests without network/Ollama. Instead we test:

  * should_verify() trigger logic — pure function, no I/O
  * Prompt construction — make sure the LLM gets the right fields
  * Result parsing — handle edge cases in the LLM's JSON output

Live LLM behavior is verified with a manual smoke test (run
test_secondary_check_live with RUN_LIVE_TESTS=1).

Run:  python -m pytest tests/test_secondary_check.py -v
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from deal_finder.appraisal.secondary_check import (
    LLM_VERIFY_HIGH_SCORE,
    LLM_VERIFY_LOW_CONF_THRESHOLD,
    VerifyResult,
    _build_user_prompt,
    should_verify,
    verify_listing,
)


# --- should_verify trigger logic -----------------------------------------


def test_should_verify_high_score_triggers():
    """Score at or above LLM_VERIFY_HIGH_SCORE always triggers a check
    regardless of confidence."""
    assert should_verify(deal_score=LLM_VERIFY_HIGH_SCORE, confidence_label="high")
    assert should_verify(deal_score=LLM_VERIFY_HIGH_SCORE, confidence_label="medium")
    assert should_verify(deal_score=LLM_VERIFY_HIGH_SCORE, confidence_label="low")
    assert should_verify(deal_score=99, confidence_label="high")


def test_should_verify_low_conf_threshold():
    """Mid-range scores trigger ONLY if confidence is 'low'."""
    s = LLM_VERIFY_LOW_CONF_THRESHOLD
    assert should_verify(deal_score=s, confidence_label="low")
    assert not should_verify(deal_score=s, confidence_label="medium")
    assert not should_verify(deal_score=s, confidence_label="high")


def test_should_verify_below_thresholds():
    """Low scores never trigger a check (saves LLM budget on
    obviously-not-deals)."""
    assert not should_verify(deal_score=50, confidence_label="low")
    assert not should_verify(deal_score=50, confidence_label="high")
    assert not should_verify(deal_score=0, confidence_label="low")


def test_should_verify_handles_none():
    """Unscoreable listings (deal_score=None) are not verified."""
    assert not should_verify(deal_score=None, confidence_label="low")
    assert not should_verify(deal_score=None, confidence_label=None)


def test_should_verify_disabled_by_env(monkeypatch):
    """LLM_SECONDARY_CHECK=0 disables all checking."""
    monkeypatch.setenv("LLM_SECONDARY_CHECK", "0")
    # Need to re-import to pick up env change
    import importlib

    import deal_finder.appraisal.secondary_check as sc
    importlib.reload(sc)
    try:
        assert not sc.should_verify(deal_score=99, confidence_label="low")
    finally:
        # Restore for other tests
        monkeypatch.setenv("LLM_SECONDARY_CHECK", "1")
        importlib.reload(sc)


# --- Prompt construction -------------------------------------------------


def test_build_user_prompt_includes_all_signals():
    """The user prompt must include title, asking, description excerpt,
    comp data, and statistical score so the LLM has full context."""
    prompt = _build_user_prompt(
        title="iPhone 11 128GB",
        description="Great condition, comes with charger",
        asking_price=200.0,
        comp_median=400.0,
        comp_sample_size=12,
        deal_score=85,
    )
    assert "iPhone 11 128GB" in prompt
    assert "$200" in prompt
    assert "Great condition" in prompt
    assert "median $400" in prompt or "$400" in prompt
    assert "n=12" in prompt
    assert "85/100" in prompt


def test_build_user_prompt_truncates_long_description():
    """Descriptions over ~1500 chars get truncated so we don't blow
    out the LLM context window or pay for irrelevant text."""
    long_desc = "x " * 2000  # 4000 chars
    prompt = _build_user_prompt(
        title="t", description=long_desc, asking_price=10.0,
        comp_median=100.0, comp_sample_size=5, deal_score=80,
    )
    assert "[truncated]" in prompt
    assert len(prompt) < 3000


def test_build_user_prompt_handles_missing_comps():
    """No comp data → graceful 'no comp data available' message rather
    than a divide-by-zero or KeyError."""
    prompt = _build_user_prompt(
        title="iPhone", description="", asking_price=200.0,
        comp_median=None, comp_sample_size=None, deal_score=70,
    )
    assert "No comp data available" in prompt


def test_build_user_prompt_no_description():
    """Empty description shouldn't break prompt construction."""
    prompt = _build_user_prompt(
        title="iPhone", description="", asking_price=200.0,
        comp_median=400.0, comp_sample_size=10, deal_score=80,
    )
    assert "(no description)" in prompt


# --- Result parsing ------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for OllamaResponse."""
    def __init__(self, parsed, raw_text="", model="test", elapsed_s=1.0):
        self.parsed = parsed
        self.raw_text = raw_text
        self.model = model
        self.elapsed_s = elapsed_s


def _patch_llm(parsed):
    """Helper: patch the Ollama client to return a synthetic response."""
    from deal_finder.appraisal import secondary_check as sc

    fake = _FakeResponse(parsed)
    return patch.object(
        sc, "get_default_client",
        return_value=type("C", (), {"generate_json": lambda self, **kw: fake})(),
    )


def test_verify_legit_verdict():
    with _patch_llm({
        "verdict": "legit",
        "concern": None,
        "confidence": "high",
    }):
        r = verify_listing(
            title="iPhone 11 128GB", description="Like new",
            asking_price=200.0, comp_median=400.0, comp_sample_size=10,
            deal_score=88, confidence_label="medium",
        )
    assert r.verdict == "legit"
    assert r.concern is None       # legit verdicts strip the concern field
    assert r.confidence == "high"


def test_verify_suspect_verdict_keeps_concern():
    with _patch_llm({
        "verdict": "suspect",
        "concern": "$40 is per-month rental, not sale price",
        "confidence": "high",
    }):
        r = verify_listing(
            title="2018 Honda Civic", description="$40 a month, low km",
            asking_price=40.0, comp_median=18000.0, comp_sample_size=15,
            deal_score=99, confidence_label="medium",
        )
    assert r.verdict == "suspect"
    assert "rental" in (r.concern or "")
    assert r.confidence == "high"


def test_verify_uncertain_verdict():
    with _patch_llm({
        "verdict": "uncertain",
        "concern": "Description too short to verify",
        "confidence": "low",
    }):
        r = verify_listing(
            title="x", description="cheap", asking_price=50.0,
            comp_median=500.0, comp_sample_size=5, deal_score=92,
            confidence_label="low",
        )
    assert r.verdict == "uncertain"
    assert r.concern is not None


def test_verify_unknown_verdict_normalized_to_uncertain():
    """LLM returning a verdict outside our enum gets normalized to
    'uncertain' so callers can still match on it."""
    with _patch_llm({
        "verdict": "maybe",
        "concern": "weird",
        "confidence": "medium",
    }):
        r = verify_listing(
            title="x", description="", asking_price=10.0,
            comp_median=100.0, comp_sample_size=10, deal_score=85,
            confidence_label="medium",
        )
    assert r.verdict == "uncertain"


def test_verify_handles_empty_parse_result():
    """If Ollama returns parsed=None (JSON decode failed), default
    to uncertain rather than crashing."""
    with _patch_llm(None):
        r = verify_listing(
            title="x", description="", asking_price=10.0,
            comp_median=100.0, comp_sample_size=10, deal_score=85,
            confidence_label=None,
        )
    assert r.verdict == "uncertain"


def test_verify_handles_llm_exception():
    """Network / connection errors must NOT propagate up — the pipeline
    should keep going with verdict='skipped'."""
    from deal_finder.appraisal import secondary_check as sc

    class _BoomClient:
        def generate_json(self, **kw):
            raise ConnectionError("ollama is down")

    with patch.object(sc, "get_default_client", return_value=_BoomClient()):
        r = verify_listing(
            title="x", description="", asking_price=10.0,
            comp_median=100.0, comp_sample_size=10, deal_score=99,
            confidence_label="high",
        )
    assert r.verdict == "skipped"
    assert r.concern is None


def test_verify_concern_truncated():
    """Defensive: if the LLM produces a 5000-char concern, we truncate
    so it fits in the appraisal_note column."""
    long_concern = "rental " * 100  # 700 chars
    with _patch_llm({
        "verdict": "suspect",
        "concern": long_concern,
        "confidence": "high",
    }):
        r = verify_listing(
            title="x", description="", asking_price=10.0,
            comp_median=100.0, comp_sample_size=10, deal_score=99,
            confidence_label="high",
        )
    assert r.verdict == "suspect"
    assert len(r.concern or "") <= 300


def test_verify_disabled_returns_skipped(monkeypatch):
    """When LLM_SECONDARY_CHECK=0 the function returns skipped without
    even invoking the LLM."""
    monkeypatch.setenv("LLM_SECONDARY_CHECK", "0")
    import importlib

    import deal_finder.appraisal.secondary_check as sc
    importlib.reload(sc)
    try:
        # Don't patch the LLM — if it's invoked, the test would either
        # hang or hit a real Ollama. should_verify should short-circuit.
        r = sc.verify_listing(
            title="x", description="", asking_price=10.0,
            comp_median=100.0, comp_sample_size=10, deal_score=99,
            confidence_label="high",
        )
        assert r.verdict == "skipped"
    finally:
        monkeypatch.setenv("LLM_SECONDARY_CHECK", "1")
        importlib.reload(sc)
