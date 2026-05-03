"""Tests for the MiniMax cloud LLM client.

Network paths can't run without MINIMAX_API_KEY, so they're gated by
RUN_LIVE_TESTS=1 + the key. Non-network logic — daily-budget
arithmetic, response parsing, error fallbacks — is fully unit-tested.

Run:  python -m pytest tests/test_minimax_client.py -v
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


# --- available() -------------------------------------------------------


def test_available_false_without_key(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    from deal_finder.appraisal import minimax_client
    assert not minimax_client.available()


def test_available_true_with_key(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "minimax_test_key_abc123")
    from deal_finder.appraisal import minimax_client
    assert minimax_client.available()


# --- daily budget logic ------------------------------------------------


def test_budget_remaining_full_with_zero_calls():
    from deal_finder.appraisal import minimax_client
    with patch.object(minimax_client, "daily_calls_used_today", return_value=0):
        assert minimax_client.daily_budget_remaining() == minimax_client.DEFAULT_DAILY_BUDGET


def test_budget_remaining_drained_at_cap():
    from deal_finder.appraisal import minimax_client
    cap = minimax_client.DEFAULT_DAILY_BUDGET
    with patch.object(minimax_client, "daily_calls_used_today", return_value=cap):
        assert minimax_client.daily_budget_remaining() == 0


def test_budget_remaining_clamps_negative_to_zero():
    """If we somehow recorded MORE calls than the cap (cap was lowered
    mid-day), don't return negative — clamp to 0."""
    from deal_finder.appraisal import minimax_client
    with patch.object(minimax_client, "daily_calls_used_today", return_value=50):
        assert minimax_client.daily_budget_remaining() == 0


def test_budget_respects_env_override(monkeypatch):
    monkeypatch.setenv("MINIMAX_DAILY_BUDGET", "5")
    from deal_finder.appraisal import minimax_client
    with patch.object(minimax_client, "daily_calls_used_today", return_value=3):
        assert minimax_client.daily_budget_remaining() == 2


def test_budget_invalid_env_uses_default(monkeypatch):
    """A garbage MINIMAX_DAILY_BUDGET value falls back to the default."""
    monkeypatch.setenv("MINIMAX_DAILY_BUDGET", "not-a-number")
    from deal_finder.appraisal import minimax_client
    with patch.object(minimax_client, "daily_calls_used_today", return_value=0):
        assert minimax_client.daily_budget_remaining() == minimax_client.DEFAULT_DAILY_BUDGET


# --- verify() — without network ----------------------------------------


def test_verify_returns_skipped_when_no_key(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    from deal_finder.appraisal import minimax_client
    r = minimax_client.verify(
        title="x", description="", asking_price=10.0,
        comp_median=100.0, comp_sample_size=10, deal_score=99,
    )
    assert r.verdict == "skipped"


def test_verify_returns_skipped_when_budget_exhausted(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    from deal_finder.appraisal import minimax_client
    with patch.object(minimax_client, "daily_budget_remaining", return_value=0):
        r = minimax_client.verify(
            title="x", description="", asking_price=10.0,
            comp_median=100.0, comp_sample_size=10, deal_score=99,
        )
    assert r.verdict == "skipped"
    assert r.backend == "minimax"


def test_verify_handles_network_error(monkeypatch):
    """A connection refused / timeout should never propagate up. Returns
    skipped + records the failed call so it doesn't blow the budget."""
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    from deal_finder.appraisal import minimax_client
    import requests as real_requests

    def boom(*a, **kw):
        raise real_requests.ConnectionError("network down")

    with patch.object(minimax_client, "daily_budget_remaining", return_value=10), \
         patch.object(minimax_client.requests, "post", side_effect=boom), \
         patch.object(minimax_client, "_record_call") as rec:
        r = minimax_client.verify(
            title="x", description="", asking_price=10.0,
            comp_median=100.0, comp_sample_size=10, deal_score=99,
        )
    assert r.verdict == "skipped"
    # We DO record a failed call so a runaway loop hitting a dead
    # endpoint can't bypass the budget.
    rec.assert_called_once()
    assert rec.call_args.kwargs.get("success") is False


def test_verify_handles_auth_error(monkeypatch):
    """401/403 → skipped + log error + record failed call."""
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    from deal_finder.appraisal import minimax_client

    fake_resp = MagicMock(status_code=401, text="invalid api key")
    with patch.object(minimax_client, "daily_budget_remaining", return_value=10), \
         patch.object(minimax_client.requests, "post", return_value=fake_resp), \
         patch.object(minimax_client, "_record_call"):
        r = minimax_client.verify(
            title="x", description="", asking_price=10.0,
            comp_median=100.0, comp_sample_size=10, deal_score=99,
        )
    assert r.verdict == "skipped"


def test_verify_parses_successful_response(monkeypatch):
    """Happy path: 200 OK with proper JSON content → parsed verdict."""
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    from deal_finder.appraisal import minimax_client

    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": '{"verdict": "suspect", "concern": "rental in disguise", "confidence": "high"}',
            },
        }],
    }
    with patch.object(minimax_client, "daily_budget_remaining", return_value=10), \
         patch.object(minimax_client.requests, "post", return_value=fake_resp), \
         patch.object(minimax_client, "_record_call"):
        r = minimax_client.verify(
            title="2018 Civic", description="",
            asking_price=40.0, comp_median=18000.0, comp_sample_size=15,
            deal_score=99,
        )
    assert r.verdict == "suspect"
    assert r.concern == "rental in disguise"
    assert r.confidence == "high"
    assert r.backend == "minimax"


def test_verify_unknown_verdict_normalized(monkeypatch):
    """API returning an unrecognized verdict gets clamped to 'uncertain'."""
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    from deal_finder.appraisal import minimax_client

    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": '{"verdict": "maybe-bad", "concern": null, "confidence": "low"}'}}],
    }
    with patch.object(minimax_client, "daily_budget_remaining", return_value=10), \
         patch.object(minimax_client.requests, "post", return_value=fake_resp), \
         patch.object(minimax_client, "_record_call"):
        r = minimax_client.verify(
            title="x", description="", asking_price=10.0,
            comp_median=100.0, comp_sample_size=10, deal_score=99,
        )
    assert r.verdict == "uncertain"


def test_verify_handles_malformed_json(monkeypatch):
    """LLM returns 200 with garbled JSON content → return uncertain
    rather than crashing."""
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    from deal_finder.appraisal import minimax_client

    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": "this is not JSON"}}],
    }
    with patch.object(minimax_client, "daily_budget_remaining", return_value=10), \
         patch.object(minimax_client.requests, "post", return_value=fake_resp), \
         patch.object(minimax_client, "_record_call"):
        r = minimax_client.verify(
            title="x", description="", asking_price=10.0,
            comp_median=100.0, comp_sample_size=10, deal_score=99,
        )
    assert r.verdict == "uncertain"


# --- live test (gated) -------------------------------------------------

LIVE_ENABLED = (
    os.environ.get("RUN_LIVE_TESTS") == "1"
    and os.environ.get("MINIMAX_API_KEY")
)


@pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="live MiniMax tests need RUN_LIVE_TESTS=1 + MINIMAX_API_KEY",
)
def test_minimax_live_smoke():
    """Smoke-test the real API: send a known-suspect listing
    ('2018 Honda Civic, $40 down, finance available') and verify
    MiniMax returns 'suspect'.

    Costs 1 of your daily budget. Records a minimax_call event."""
    from deal_finder.appraisal import minimax_client

    r = minimax_client.verify(
        title="2018 Honda Civic LX",
        description="$40 down, finance available OAC. Bi-weekly $95.",
        asking_price=40.0,
        comp_median=18000.0,
        comp_sample_size=15,
        deal_score=99,
        confidence_label="medium",
    )
    print(f"\nMiniMax verdict: {r.verdict}")
    print(f"Concern        : {r.concern}")
    print(f"Confidence     : {r.confidence}")
    print(f"Elapsed        : {r.elapsed_s:.2f}s")
    assert r.verdict in ("suspect", "uncertain"), (
        f"expected suspect or uncertain for an obvious financing-scheme listing, got {r.verdict}"
    )
