"""Rejection filter.

Two-stage gate run after price extraction, before deduplication and DB
write. Listings that match either stage get persisted with rejected=True
and a rejection_reason, but skip the eBay/LLM appraisal pipeline.

  Stage 1: regex patterns matched against title + " " + description (lowered)
  Stage 2: substring keywords matched against title only (lowered)

Patterns and keywords live in plain-text config files:
  config/rejection_patterns.txt
  config/rejection_keywords.txt

Both support # comments and blank lines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


# Resolve config dir relative to project root. Override with
# DEAL_FINDER_CONFIG_DIR env var if running outside the repo.
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


@dataclass(frozen=True)
class RejectionResult:
    """Outcome of running the rejection filter.

    rejected -- True if either stage matched
    reason   -- e.g. "pattern: \\bswap\\b" or "keyword: parts only"; None when
                rejected is False
    """
    rejected: bool
    reason: str | None


def _read_config_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


@lru_cache(maxsize=4)
def _load_patterns(config_dir: str) -> list[re.Pattern[str]]:
    cdir = Path(config_dir)
    raws = _read_config_lines(cdir / "rejection_patterns.txt")
    compiled: list[re.Pattern[str]] = []
    for raw in raws:
        try:
            compiled.append(re.compile(raw, re.IGNORECASE))
        except re.error:
            # Skip bad regex lines silently — config errors shouldn't kill
            # the pipeline. (We log them in production via a logger; the
            # spike module stays log-free.)
            continue
    return compiled


@lru_cache(maxsize=4)
def _load_keywords(config_dir: str) -> tuple[str, ...]:
    cdir = Path(config_dir)
    raws = _read_config_lines(cdir / "rejection_keywords.txt")
    return tuple(k.lower() for k in raws)


def evaluate(
    title: str,
    description: str | None = None,
    *,
    config_dir: Path | str | None = None,
) -> RejectionResult:
    """Run both stages. Returns RejectionResult.

    config_dir defaults to <repo>/config, but tests can override it.
    """
    cdir = str(config_dir) if config_dir else str(_DEFAULT_CONFIG_DIR)

    title_l = (title or "").lower()
    combined_l = (title_l + " " + (description or "").lower()).strip()

    # Stage 1: regex patterns against title + description
    for pat in _load_patterns(cdir):
        if pat.search(combined_l):
            return RejectionResult(
                rejected=True, reason=f"pattern: {pat.pattern}"
            )

    # Stage 2: keyword substring against title only
    for kw in _load_keywords(cdir):
        if kw in title_l:
            return RejectionResult(
                rejected=True, reason=f"keyword: {kw}"
            )

    return RejectionResult(rejected=False, reason=None)


def reset_cache() -> None:
    """Clear the LRU caches. Useful for tests that swap config dirs."""
    _load_patterns.cache_clear()
    _load_keywords.cache_clear()
