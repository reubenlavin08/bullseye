"""Tests for the regex-only path of the condition extractor.

We run with use_llm=False so tests are deterministic and don't require
Ollama to be running. The hybrid path (regex ∪ LLM) is exercised in
production; here we validate the regex bank alone catches obvious cases.

Run with:  python -m pytest tests/test_condition_signals.py -v
"""
from __future__ import annotations

from deal_finder.appraisal.condition_signals import (
    SCORE_ADJUSTMENTS,
    extract_condition_signals,
)


def _extract(desc: str):
    return extract_condition_signals(desc, use_llm=False)


# --- The user-reported Malibu case -------------------------------------

def test_malibu_brake_noise_dent_scratches():
    """The exact 2010 Malibu description that slipped through before."""
    desc = (
        "Runs fine but now makes a loud noise when braking. "
        "The car has a very small dent and minor paint scratches."
    )
    r = _extract(desc)
    assert "needs_repair" in r.flags_fired
    assert "cosmetic_damage" in r.flags_fired
    assert r.score_adjustment <= -15  # at least the needs_repair penalty


# --- needs_repair ------------------------------------------------------

def test_loud_noise_when_braking():
    r = _extract("Brakes make loud noise when stopping at lights")
    assert "needs_repair" in r.flags_fired


def test_check_engine_light():
    r = _extract("Drives well, just check engine light is on")
    assert "needs_repair" in r.flags_fired


def test_wont_start():
    r = _extract("Selling for parts. Won't start.")
    assert "needs_repair" in r.flags_fired


def test_oil_leak():
    r = _extract("Minor oil leak, otherwise solid")
    assert "needs_repair" in r.flags_fired


def test_battery_doesnt_hold_charge():
    r = _extract("Phone is mint but battery doesn't hold a charge anymore.")
    assert "needs_repair" in r.flags_fired


def test_cracked_screen():
    r = _extract("Working iPhone 13 with a cracked screen.")
    assert "needs_repair" in r.flags_fired


# --- accident_history --------------------------------------------------

def test_minor_accident_history():
    r = _extract("Bought used a year ago, has a minor accident history "
                 "from before I owned it.")
    assert "accident_history" in r.flags_fired


def test_rear_ended():
    r = _extract("Was rear-ended in a parking lot. Bumper repaired.")
    assert "accident_history" in r.flags_fired


# --- salvage_title -----------------------------------------------------

def test_salvage_title():
    r = _extract("Drives perfect, salvage title from minor flood.")
    assert "salvage_title" in r.flags_fired


def test_rebuilt_title():
    r = _extract("Rebuilt title, fully restored.")
    assert "salvage_title" in r.flags_fired


# --- high_mileage ------------------------------------------------------

def test_high_mileage_explicit():
    r = _extract("High mileage but engine still runs strong.")
    assert "high_mileage" in r.flags_fired


def test_high_mileage_number():
    r = _extract("2008 Civic, 240,000 km, runs fine.")
    assert "high_mileage" in r.flags_fired


def test_low_mileage_does_NOT_fire_high_mileage():
    r = _extract("60,000 km, garage kept.")
    assert "high_mileage" not in r.flags_fired


# --- cosmetic_damage ---------------------------------------------------

def test_small_dent():
    r = _extract("Small dent on driver door, otherwise clean.")
    assert "cosmetic_damage" in r.flags_fired


def test_paint_scratches():
    r = _extract("Some paint scratches on the hood.")
    assert "cosmetic_damage" in r.flags_fired


def test_rust():
    r = _extract("Surface rust on rear panel.")
    assert "cosmetic_damage" in r.flags_fired


def test_clean_does_NOT_fire_cosmetic():
    r = _extract("Clean, no scratches or dents anywhere.")
    # 'no scratches' should NOT trigger via simple substring match
    # (ours uses '\bsmall|minor|few...\s+scratches'). Verify:
    assert "cosmetic_damage" not in r.flags_fired


# --- missing_parts -----------------------------------------------------

def test_no_charger():
    r = _extract("MacBook Pro 2020. No charger included.")
    assert "missing_parts" in r.flags_fired


def test_for_parts_only():
    r = _extract("For parts only, doesn't power on.")
    assert "missing_parts" in r.flags_fired


# --- positive signals --------------------------------------------------

def test_mint_condition():
    r = _extract("Mint condition, single owner from new.")
    assert "excellent_condition" in r.flags_fired
    assert "low_use" in r.flags_fired


def test_garage_kept():
    r = _extract("Garage kept, low miles for the year.")
    assert "low_use" in r.flags_fired


def test_brand_new():
    r = _extract("Brand new in box.")
    assert "excellent_condition" in r.flags_fired


def test_warranty():
    r = _extract("Active manufacturer warranty until 2027.")
    assert "has_warranty" in r.flags_fired


def test_recently_serviced():
    r = _extract("Just serviced, new tires and brakes.")
    assert "recently_serviced" in r.flags_fired


# --- adjustment math ---------------------------------------------------

def test_adjustment_clamped_negative():
    """A truly broken listing shouldn't drag below MAX_NEGATIVE_ADJ."""
    r = _extract(
        "Salvage title, accident history, dent, scratches, missing parts, "
        "needs major repair."
    )
    assert r.score_adjustment >= -35  # clamped


def test_adjustment_zero_for_neutral_description():
    r = _extract("2018 model, runs as expected, ready to drive.")
    assert r.score_adjustment == 0
    assert r.flags_fired == []


def test_score_adjustments_sum_correctly():
    r = _extract(
        "Runs fine but now makes a loud noise when braking. "
        "The car has a very small dent and minor paint scratches."
    )
    expected = (
        SCORE_ADJUSTMENTS["needs_repair"]
        + SCORE_ADJUSTMENTS["cosmetic_damage"]
    )
    assert r.score_adjustment == expected
