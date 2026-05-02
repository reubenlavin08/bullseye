"""Tests for the watch-batching coordinator.

The coordinator combines K stalest watches into one FB search and
attributes each returned listing back to the most-specific matching
watch. Attribution must be deterministic and prefer specificity.

Run with:  python -m pytest tests/test_batching.py -v
"""
from __future__ import annotations

from deal_finder.scheduler.jobs import attribute_listing


def test_attributes_to_most_specific_keyword():
    batch = [
        {"id": 1, "keyword": "arduino"},
        {"id": 2, "keyword": "arduino uno"},
    ]
    assert attribute_listing("Arduino Uno R4 starter kit", batch)["id"] == 2
    assert attribute_listing("Arduino Mega 2560", batch)["id"] == 1


def test_attributes_each_watch_correctly():
    batch = [
        {"id": 1, "keyword": "arduino"},
        {"id": 2, "keyword": "raspberry pi"},
        {"id": 3, "keyword": "esp32"},
        {"id": 4, "keyword": "stepper motor"},
    ]
    cases = [
        ("Arduino Uno R4 starter kit", 1),
        ("Raspberry Pi 5 8GB new", 2),
        ("ESP32 dev board cheap", 3),
        ("NEMA 17 stepper motor", 4),
    ]
    for title, expected in cases:
        got = attribute_listing(title, batch)
        assert got is not None and got["id"] == expected, (
            f"{title!r}: got {got['id'] if got else None}, expected {expected}"
        )


def test_no_match_returns_none():
    """Listings that don't match any watch keyword in the batch get
    dropped — they're false positives from FB's loose tokenization
    when we combine multiple keywords."""
    batch = [
        {"id": 1, "keyword": "arduino"},
        {"id": 2, "keyword": "raspberry pi"},
    ]
    assert attribute_listing("Yamaha keyboard", batch) is None
    assert attribute_listing("Yeah whatever", batch) is None


def test_case_insensitive():
    batch = [{"id": 1, "keyword": "Arduino"}]
    assert attribute_listing("ARDUINO UNO", batch)["id"] == 1
    assert attribute_listing("arduino uno", batch)["id"] == 1


def test_multi_word_requires_all_words():
    """Watch keyword 'electric scooter' should only match listings that
    contain BOTH 'electric' AND 'scooter' — not just one."""
    batch = [{"id": 1, "keyword": "electric scooter"}]
    assert attribute_listing("Electric Scooter for kids", batch)["id"] == 1
    assert attribute_listing("Kids Scooter (no battery)", batch) is None
    assert attribute_listing("Electric Skateboard", batch) is None


def test_specificity_wins_when_substring_overlaps():
    """'stepper motor' should beat 'stepper' even though both match
    a NEMA 17 stepper motor listing."""
    batch = [
        {"id": 1, "keyword": "stepper"},
        {"id": 2, "keyword": "stepper motor"},
    ]
    got = attribute_listing("NEMA 17 stepper motor", batch)
    assert got["id"] == 2


def test_empty_batch_returns_none():
    assert attribute_listing("Anything", []) is None


def test_empty_keyword_in_batch_skipped():
    """A watch with an empty keyword (defensive) is ignored, not a
    universal-match wildcard."""
    batch = [
        {"id": 1, "keyword": ""},
        {"id": 2, "keyword": "arduino"},
    ]
    assert attribute_listing("Arduino Uno", batch)["id"] == 2
    assert attribute_listing("Random listing", batch) is None
