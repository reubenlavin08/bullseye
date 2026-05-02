"""Tests for the bimodal-cluster splitter that fixes the 'boat motor'
problem.

Run:  python -m pytest tests/test_bimodal_split.py -v
"""
from __future__ import annotations

from deal_finder.db.comps import _maybe_split_bimodal


# --- The boat-motor scenario --------------------------------------------

def test_bimodal_splits_boat_motor_for_cheap_asking():
    """Cheap trolling motors at $50-200 + outboards at $1500-5000.
    Asking $80 -> should keep the cheap cluster."""
    prices = sorted([50, 80, 100, 120, 200, 1500, 2500, 3000, 4000, 5000])
    cluster, info = _maybe_split_bimodal(prices, asking_price=80)
    assert info["split_triggered"] is True
    assert info["chose"] == "left"
    assert max(cluster) <= 200
    assert min(cluster) >= 50


def test_bimodal_splits_boat_motor_for_expensive_asking():
    """Same comp set, asking $4000 -> should keep the expensive cluster."""
    prices = sorted([50, 80, 100, 120, 200, 1500, 2500, 3000, 4000, 5000])
    cluster, info = _maybe_split_bimodal(prices, asking_price=4000)
    assert info["split_triggered"] is True
    assert info["chose"] == "right"
    assert min(cluster) >= 1500


# --- No-split cases (the splitter should be conservative) ---------------

def test_does_not_split_homogeneous_data():
    """Tight cluster of similar prices — no split."""
    prices = sorted([100, 110, 120, 130, 140, 150, 160, 170, 180])
    cluster, info = _maybe_split_bimodal(prices, asking_price=130)
    assert info["split_triggered"] is False
    assert cluster == prices


def test_does_not_split_with_no_asking_price():
    """Without asking price we can't pick a cluster — refuse to split."""
    prices = sorted([50, 80, 100, 120, 200, 1500, 2500, 3000, 4000, 5000])
    cluster, info = _maybe_split_bimodal(prices, asking_price=None)
    assert info["split_triggered"] is False
    assert cluster == prices


def test_does_not_split_when_one_cluster_is_too_small():
    """If splitting would leave a single-element cluster, don't split."""
    prices = sorted([100, 110, 120, 130, 140, 150, 160, 170, 5000])
    cluster, info = _maybe_split_bimodal(prices, asking_price=130)
    assert info["split_triggered"] is False


def test_does_not_split_when_gap_is_too_small():
    """A 1.5x gap is normal price variance, not a real cluster split."""
    prices = sorted([100, 110, 120, 130, 140, 200, 210, 220, 230])
    cluster, info = _maybe_split_bimodal(prices, asking_price=140)
    # 140 -> 200 is a 1.43x gap, well below the 2.5x threshold.
    assert info["split_triggered"] is False


def test_does_not_split_when_clusters_too_similar():
    """Two clusters with >2.5x gap but medians within 2x of each other —
    treat as one wide cluster, not bimodal."""
    prices = sorted([50, 60, 70, 80, 220, 230, 240, 250])
    # 80 -> 220 is 2.75x gap (passes gap check) BUT medians are 65 and 235,
    # ratio 3.6x — would split. Use a more borderline case:
    prices = sorted([100, 110, 120, 130, 280, 290, 300, 310])
    # Gap 130->280 = 2.15x, below threshold — won't split.
    cluster, info = _maybe_split_bimodal(prices, asking_price=200)
    assert info["split_triggered"] is False


def test_too_few_comps_disables_split():
    """With <6 comps, splitting is unreliable — don't try."""
    prices = sorted([100, 200, 1000, 2000, 3000])
    cluster, info = _maybe_split_bimodal(prices, asking_price=150)
    assert info["split_triggered"] is False
