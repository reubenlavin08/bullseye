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
    """Helper: patch the Ollama client to return a synthetic response.
    Also patches MiniMax to be unavailable so tests stay deterministic
    (no escalation unless the test explicitly opts in)."""
    from deal_finder.appraisal import minimax_client, secondary_check as sc

    fake = _FakeResponse(parsed)
    return _MultiPatch([
        patch.object(
            sc, "get_default_client",
            return_value=type("C", (), {"generate_json": lambda self, **kw: fake})(),
        ),
        patch.object(minimax_client, "available", return_value=False),
    ])


class _MultiPatch:
    """Context manager that activates multiple patches together."""

    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.__enter__()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.__exit__(*exc)


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


# --- Tiered escalation: Ollama → MiniMax ---------------------------------


def test_should_escalate_when_tier1_uncertain_and_score_high_enough():
    """Tier 1 returning 'uncertain' should escalate ONLY when the score
    is high enough that the listing would actually email. Below that
    threshold, even uncertain doesn't matter."""
    from deal_finder.appraisal import minimax_client, secondary_check as sc

    with patch.object(minimax_client, "available", return_value=True), \
         patch.object(minimax_client, "daily_budget_remaining", return_value=10):
        # uncertain + score=90 (== LLM_CLOUD_UNCERTAIN_MIN_SCORE) → escalate
        should, reason = sc._should_escalate_to_cloud(
            tier1_verdict="uncertain", deal_score=90, confidence_label="medium",
        )
    assert should
    assert "uncertain" in reason


def test_should_NOT_escalate_when_uncertain_below_min_score():
    """Tier 1 uncertain at score=85 → don't escalate. Saves a token
    on a listing that wouldn't email anyway."""
    from deal_finder.appraisal import minimax_client, secondary_check as sc

    with patch.object(minimax_client, "available", return_value=True), \
         patch.object(minimax_client, "daily_budget_remaining", return_value=10):
        should, reason = sc._should_escalate_to_cloud(
            tier1_verdict="uncertain", deal_score=85, confidence_label="medium",
        )
    assert not should
    assert "sufficient" in reason


def test_should_escalate_when_score_at_force_threshold():
    """Score >= LLM_CLOUD_FORCE_SCORE (97) always escalates regardless
    of Tier 1's verdict. Top-tier outliers are worth one token."""
    from deal_finder.appraisal import minimax_client, secondary_check as sc

    with patch.object(minimax_client, "available", return_value=True), \
         patch.object(minimax_client, "daily_budget_remaining", return_value=10):
        should, _ = sc._should_escalate_to_cloud(
            tier1_verdict="legit", deal_score=97, confidence_label="high",
        )
    assert should


def test_should_NOT_escalate_at_high_score_below_force():
    """Score=95 (under 97 force-threshold), tier1=legit → trust tier1.
    The old logic would escalate here, but we tightened it."""
    from deal_finder.appraisal import minimax_client, secondary_check as sc

    with patch.object(minimax_client, "available", return_value=True), \
         patch.object(minimax_client, "daily_budget_remaining", return_value=10):
        should, _ = sc._should_escalate_to_cloud(
            tier1_verdict="legit", deal_score=95, confidence_label="high",
        )
    assert not should


def test_should_escalate_low_conf_high_score_when_tier1_uncertain():
    """Score >= 90 + low confidence + tier1 NOT legit → escalate.
    Sparse comp data + high score is the false-positive zone."""
    from deal_finder.appraisal import minimax_client, secondary_check as sc

    with patch.object(minimax_client, "available", return_value=True), \
         patch.object(minimax_client, "daily_budget_remaining", return_value=10):
        should, _ = sc._should_escalate_to_cloud(
            tier1_verdict="uncertain", deal_score=92, confidence_label="low",
        )
    assert should


def test_should_NOT_escalate_low_conf_when_tier1_legit():
    """Score 92 + low conf + tier1=LEGIT → don't escalate. Tier 1's
    confident yes-vote on a high-score-but-low-statistical-confidence
    listing is enough — saves a token."""
    from deal_finder.appraisal import minimax_client, secondary_check as sc

    with patch.object(minimax_client, "available", return_value=True), \
         patch.object(minimax_client, "daily_budget_remaining", return_value=10):
        should, reason = sc._should_escalate_to_cloud(
            tier1_verdict="legit", deal_score=92, confidence_label="low",
        )
    assert not should
    assert "sufficient" in reason


def test_should_NOT_escalate_when_minimax_unavailable():
    """No API key → never escalate even if other conditions met."""
    from deal_finder.appraisal import minimax_client, secondary_check as sc

    with patch.object(minimax_client, "available", return_value=False):
        should, reason = sc._should_escalate_to_cloud(
            tier1_verdict="uncertain", deal_score=99, confidence_label="low",
        )
    assert not should
    assert "unavailable" in reason


def test_should_NOT_escalate_when_budget_exhausted():
    """Budget=0 → don't escalate even when Tier 1 was uncertain."""
    from deal_finder.appraisal import minimax_client, secondary_check as sc

    with patch.object(minimax_client, "available", return_value=True), \
         patch.object(minimax_client, "daily_budget_remaining", return_value=0):
        should, reason = sc._should_escalate_to_cloud(
            tier1_verdict="uncertain", deal_score=99, confidence_label="low",
        )
    assert not should
    assert "budget" in reason


def test_should_NOT_escalate_when_tier1_legit_and_score_below_threshold():
    """Tier 1 said legit and score < 95 → cheap path, save the
    MiniMax call."""
    from deal_finder.appraisal import minimax_client, secondary_check as sc

    with patch.object(minimax_client, "available", return_value=True), \
         patch.object(minimax_client, "daily_budget_remaining", return_value=10):
        should, reason = sc._should_escalate_to_cloud(
            tier1_verdict="legit", deal_score=87, confidence_label="medium",
        )
    assert not should
    assert "sufficient" in reason


def test_verify_listing_uses_tier2_verdict_when_escalated():
    """End-to-end: Tier 1 says uncertain on score=92, low conf →
    Trigger C fires → MiniMax says suspect → final result is suspect
    with backend='minimax'."""
    from deal_finder.appraisal import minimax_client, secondary_check as sc

    fake_ollama_resp = _FakeResponse({
        "verdict": "uncertain",
        "concern": "not enough info",
        "confidence": "low",
    })
    fake_ollama_client = type("C", (), {
        "generate_json": lambda self, **kw: fake_ollama_resp,
    })()

    fake_tier2 = minimax_client.VerifyResult(
        verdict="suspect",
        concern="financing scheme detected",
        confidence="high",
        elapsed_s=1.5,
        model="MiniMax-Test",
        backend="minimax",
    )

    with patch.object(sc, "get_default_client", return_value=fake_ollama_client), \
         patch.object(minimax_client, "available", return_value=True), \
         patch.object(minimax_client, "daily_budget_remaining", return_value=10), \
         patch.object(minimax_client, "verify", return_value=fake_tier2):
        r = sc.verify_listing(
            title="x", description="y", asking_price=40.0,
            comp_median=400.0, comp_sample_size=10, deal_score=92,
            confidence_label="low",
        )
    assert r.verdict == "suspect"
    assert r.concern == "financing scheme detected"
    assert r.backend == "minimax"


def test_verify_listing_falls_back_to_tier1_when_tier2_skipped():
    """If MiniMax bails (budget race, network), use Tier 1's verdict
    rather than returning skipped. Score=99 hits force-threshold
    so escalation IS attempted, but it fails and falls back."""
    from deal_finder.appraisal import minimax_client, secondary_check as sc

    fake_ollama_resp = _FakeResponse({
        "verdict": "uncertain",
        "concern": "ambiguous",
        "confidence": "low",
    })
    fake_ollama_client = type("C", (), {
        "generate_json": lambda self, **kw: fake_ollama_resp,
    })()
    fake_tier2_skipped = minimax_client.VerifyResult(
        verdict="skipped", concern=None, confidence=None,
        elapsed_s=0.0, model=None, backend="minimax",
    )

    with patch.object(sc, "get_default_client", return_value=fake_ollama_client), \
         patch.object(minimax_client, "available", return_value=True), \
         patch.object(minimax_client, "daily_budget_remaining", return_value=10), \
         patch.object(minimax_client, "verify", return_value=fake_tier2_skipped):
        r = sc.verify_listing(
            title="x", description="", asking_price=40.0,
            comp_median=400.0, comp_sample_size=10, deal_score=99,
            confidence_label="low",
        )
    # Tier 1 said uncertain → final is uncertain via fallback
    assert r.verdict == "uncertain"
    assert r.backend == "ollama"


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
