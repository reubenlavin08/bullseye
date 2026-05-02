"""Tests for the rejection filter.

Run with:  python -m pytest tests/test_rejection.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from deal_finder.scraper import rejection
from deal_finder.scraper.rejection import evaluate


@pytest.fixture(autouse=True)
def _reset_cache():
    rejection.reset_cache()
    yield
    rejection.reset_cache()


@pytest.fixture
def fake_config_dir(tmp_path: Path) -> Path:
    """Build a clean config dir per test so we don't depend on the repo's
    real config files (which evolve)."""
    (tmp_path / "rejection_patterns.txt").write_text(
        "\n".join([
            r"\btrades?\b",
            r"\bswap\b",
            r"\bISO\b",
            r"\blooking for\b",
            r"\bwanted\b",
            r"\bwill trade\b",
            r"\bpartial trade\b",
        ]),
        encoding="utf-8",
    )
    (tmp_path / "rejection_keywords.txt").write_text(
        "\n".join([
            "curb alert",
            "lot",
            "bundle",
            "parts only",
            "for parts",
            "not working",
        ]),
        encoding="utf-8",
    )
    return tmp_path


# ---- pattern stage (title + description) --------------------------------

def test_pattern_match_in_title_only(fake_config_dir):
    r = evaluate("ISO an iPhone 14", "any description", config_dir=fake_config_dir)
    assert r.rejected is True
    assert "pattern" in r.reason


def test_pattern_match_in_description_only(fake_config_dir):
    r = evaluate("iPhone 14", "will trade for laptop", config_dir=fake_config_dir)
    assert r.rejected is True
    assert "pattern" in r.reason


def test_pattern_trade_singular(fake_config_dir):
    r = evaluate("iPhone trade?", None, config_dir=fake_config_dir)
    assert r.rejected is True


def test_pattern_trade_plural(fake_config_dir):
    r = evaluate("Open to trades", None, config_dir=fake_config_dir)
    assert r.rejected is True


def test_pattern_swap(fake_config_dir):
    r = evaluate("Swap for ps5", None, config_dir=fake_config_dir)
    assert r.rejected is True


def test_pattern_looking_for(fake_config_dir):
    r = evaluate("Looking for cheap mountain bike",
                 None, config_dir=fake_config_dir)
    assert r.rejected is True


def test_pattern_wanted(fake_config_dir):
    r = evaluate("Wanted: dirt bike under 1000",
                 None, config_dir=fake_config_dir)
    assert r.rejected is True


# ---- keyword stage (title only) -----------------------------------------

def test_keyword_curb_alert(fake_config_dir):
    r = evaluate("Curb Alert old couch", None, config_dir=fake_config_dir)
    assert r.rejected is True
    assert "keyword" in r.reason
    assert "curb alert" in r.reason


def test_keyword_parts_only(fake_config_dir):
    r = evaluate("BMW e36 parts only", None, config_dir=fake_config_dir)
    assert r.rejected is True


def test_keyword_lot(fake_config_dir):
    r = evaluate("Lot of vintage records", None, config_dir=fake_config_dir)
    assert r.rejected is True


def test_keyword_bundle(fake_config_dir):
    r = evaluate("PS4 bundle with games", None, config_dir=fake_config_dir)
    assert r.rejected is True


def test_keyword_in_description_does_not_trigger(fake_config_dir):
    # "lot" only matches in title, not description (per spec)
    r = evaluate(
        "Vintage radio",
        "comes with a lot of accessories",
        config_dir=fake_config_dir,
    )
    assert r.rejected is False


def test_keyword_case_insensitive(fake_config_dir):
    r = evaluate("FOR PARTS only — read description",
                 None, config_dir=fake_config_dir)
    assert r.rejected is True


# ---- pass-through cases -------------------------------------------------

def test_normal_listing_passes(fake_config_dir):
    r = evaluate("iPhone 14 Pro 256GB",
                 "Used 6 months, mint condition.",
                 config_dir=fake_config_dir)
    assert r.rejected is False
    assert r.reason is None


def test_word_with_substring_does_not_false_match_pattern(fake_config_dir):
    # "wanted" is a word boundary regex; "unwanted" should NOT trigger.
    r = evaluate("Removing unwanted scratches with new polish kit",
                 None, config_dir=fake_config_dir)
    assert r.rejected is False


def test_free_listing_is_not_rejected(fake_config_dir):
    # "free" is intentionally absent from the keyword list.
    r = evaluate("Free moving boxes", None, config_dir=fake_config_dir)
    assert r.rejected is False


# ---- config-loading edge cases -------------------------------------------

def test_missing_config_files_is_safe(tmp_path):
    # No files at all → nothing rejected.
    rejection.reset_cache()
    r = evaluate("ISO an iPhone", None, config_dir=tmp_path)
    assert r.rejected is False


def test_comments_and_blank_lines_ignored(tmp_path):
    (tmp_path / "rejection_patterns.txt").write_text(
        "# a comment line\n"
        "\n"
        r"\bswap\b" + "\n"
        "  # leading whitespace comment is also okay\n",
        encoding="utf-8",
    )
    (tmp_path / "rejection_keywords.txt").write_text("", encoding="utf-8")
    rejection.reset_cache()
    r = evaluate("looking to swap", None, config_dir=tmp_path)
    assert r.rejected is True


def test_invalid_regex_is_skipped_not_fatal(tmp_path):
    (tmp_path / "rejection_patterns.txt").write_text(
        "[unclosed-char-class\n"
        r"\bswap\b" + "\n",
        encoding="utf-8",
    )
    (tmp_path / "rejection_keywords.txt").write_text("", encoding="utf-8")
    rejection.reset_cache()
    # The valid pattern still works; the invalid one is silently dropped.
    r = evaluate("swap meet item", None, config_dir=tmp_path)
    assert r.rejected is True


# --- Real-world service-detection scenarios -----------------------------
# Use the actual config files so we cover the real production patterns.

def _real_config_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "config"


def test_service_with_innocent_title_is_caught_in_description():
    """Title looks like a normal item listing, but description reveals
    it's a service. This is the user-reported gap."""
    rejection.reset_cache()
    r = evaluate(
        title="Custom Audio System",
        description=(
            "I install custom audio systems in any vehicle. "
            "Rates starting at $200. DM for quote."
        ),
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True
    assert "pattern" in r.reason


def test_hourly_rate_pattern_in_description():
    rejection.reset_cache()
    r = evaluate(
        title="Mechanic services",
        description="$80/hr labour, mobile available, call for appointment",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_first_person_service_pitch():
    rejection.reset_cache()
    r = evaluate(
        title="iPhone Repair",
        description="I repair all iPhone models. Same day service.",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_legitimate_item_with_we_in_description_not_rejected():
    """Don't false-positive a legit listing that happens to use 'we'."""
    rejection.reset_cache()
    r = evaluate(
        title="Used iPhone 14 Pro 256GB",
        description="We're moving and need to sell. Mint condition.",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is False


def test_dm_for_pricing_pattern():
    rejection.reset_cache()
    r = evaluate(
        title="Window Tint",
        description="DM for pricing on full vehicle window tinting",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_monthly_fee_caught():
    rejection.reset_cache()
    r = evaluate(
        title="Storage Locker",
        description="$120 per month, climate controlled.",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True
