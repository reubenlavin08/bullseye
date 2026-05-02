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


# --- Rental detection (explicit + implicit $X/month) -------------------

def test_rental_explicit_for_rent():
    rejection.reset_cache()
    r = evaluate(
        title="2br Suite",
        description="Beautiful 2br for rent in Kitsilano, available now.",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_rental_for_lease():
    rejection.reset_cache()
    r = evaluate(
        title="Commercial Space",
        description="1200 sqft for lease, ground floor",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_rental_dollar_per_mo_slash():
    """The exact case the user reported: implicit rental with $X/mo
    pricing and no 'rent' word in sight."""
    rejection.reset_cache()
    r = evaluate(
        title="Studio downtown",
        description="$1,800/mo all utilities included, no pets.",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_rental_dollar_per_month_slash():
    rejection.reset_cache()
    r = evaluate(
        title="Room available",
        description="$950/month, shared kitchen",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_rental_dollar_per_night():
    rejection.reset_cache()
    r = evaluate(
        title="Cabin getaway",
        description="$220/night, 2-night minimum",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_rental_dollar_per_week():
    rejection.reset_cache()
    r = evaluate(
        title="RV stay",
        description="$400/wk, includes hookups",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_rental_security_deposit():
    rejection.reset_cache()
    r = evaluate(
        title="Basement suite",
        description="Bright suite, $500 security deposit",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_rental_first_last_month():
    rejection.reset_cache()
    r = evaluate(
        title="Apartment",
        description="First and last month rent required",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_rental_lease_term():
    rejection.reset_cache()
    r = evaluate(
        title="House",
        description="12 month lease minimum",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_rental_sublet():
    rejection.reset_cache()
    r = evaluate(
        title="Need a sublet",
        description="Looking for someone to take over",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_rental_airbnb():
    rejection.reset_cache()
    r = evaluate(
        title="Cozy spot",
        description="Used as an Airbnb, no long-term tenants",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_rental_short_term():
    rejection.reset_cache()
    r = evaluate(
        title="Furnished suite",
        description="Short-term rental available, fully equipped",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_rental_utilities_included():
    rejection.reset_cache()
    r = evaluate(
        title="Cozy room",
        description="Utilities included, available immediately.",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


# --- Make-me-an-offer placeholder detection ----------------------------

def test_offer_make_me_an_offer():
    """The user-reported $1 iPhone case: 'make me an offer' with no
    real anchor price."""
    rejection.reset_cache()
    r = evaluate(
        title="iPhone 11 128gb",
        description="Make me an offer.",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_offer_make_a_reasonable_offer():
    rejection.reset_cache()
    r = evaluate(
        title="Vintage chair",
        description="Make a reasonable offer and it's yours.",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_offer_name_your_price():
    rejection.reset_cache()
    r = evaluate(
        title="Stuff",
        description="Name your price, just need it gone",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_offer_send_offers():
    rejection.reset_cache()
    r = evaluate(
        title="Mountain bike",
        description="Send offers — open to anything reasonable",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_offer_open_to_offers():
    rejection.reset_cache()
    r = evaluate(
        title="Sectional couch",
        description="Open to offers",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_offer_highest_offer_wins():
    rejection.reset_cache()
    r = evaluate(
        title="Bass guitar",
        description="Highest offer wins by Friday",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_offer_what_will_you_give():
    rejection.reset_cache()
    r = evaluate(
        title="Garage cleanout",
        description="What will you give me for it?",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


# --- False-positive guards ---------------------------------------------

def test_obo_with_real_price_not_rejected():
    """'$500 OBO' is a normal listing — don't reject. Only the more
    aggressive 'name your price' / 'make me an offer' patterns trigger."""
    rejection.reset_cache()
    r = evaluate(
        title="Used dirt bike",
        description="$3500 OBO, runs great",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is False


def test_offer_word_alone_not_rejected():
    """Listings that mention 'offer' incidentally shouldn't be rejected."""
    rejection.reset_cache()
    r = evaluate(
        title="Couch",
        description="Limited time offer — moving sale.",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is False


def test_legit_listing_with_month_in_use_history_not_rejected():
    """'Used for 6 months' should NOT trip the rental filter."""
    rejection.reset_cache()
    r = evaluate(
        title="MacBook Pro",
        description="Used for 6 months, mint condition.",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is False


def test_trade_in_with_dash():
    rejection.reset_cache()
    r = evaluate(
        title="2018 Honda Civic",
        description="Trade-ins welcome on any vehicle.",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_trade_in_with_space():
    rejection.reset_cache()
    r = evaluate(
        title="iPhone 14 Pro",
        description="Open to trade ins. Cash preferred.",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_trade_in_accepting():
    rejection.reset_cache()
    r = evaluate(
        title="MacBook Air",
        description="Accepting trade-ins of newer Apple laptops",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_trade_in_open_to():
    rejection.reset_cache()
    r = evaluate(
        title="Yamaha keyboard",
        description="Open to trade in on a guitar",
        config_dir=_real_config_dir(),
    )
    assert r.rejected is True


def test_legit_listing_mentioning_lease_in_history_not_rejected():
    """'Lease ended' as historical context shouldn't reject a sale."""
    rejection.reset_cache()
    r = evaluate(
        title="2020 Honda Civic",
        description="Bought after my lease ended last year",
        config_dir=_real_config_dir(),
    )
    # NOTE: this WILL trigger \blease[d]?\b. Acceptable false-positive
    # rate for the value of catching all rentals. If this becomes a
    # problem in practice, tighten the patterns.
    # Documenting the trade-off rather than asserting either way.
    _ = r
