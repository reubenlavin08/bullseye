"""Semantic embedding utilities for comp filtering.

The boat-motor / vintage-fishing-motor problem: a single keyword search
returns wildly heterogeneous listings whose median is meaningless. Even
after Tukey trimming and bimodal cluster splits, "vintage electric
fishing motor" comps span $50-$2000 with each unit genuinely unique.

This module computes vector embeddings of the target listing and each
comp candidate, then filters comps by cosine similarity to the target.
Result: a smaller but much more relevant comp set.

Uses Ollama's `nomic-embed-text` (~250 MB, 768-dim, fast on CPU).

IMPORTANT: nomic-embed-text was trained with task-specific prefixes
(per the model's README). We MUST prefix:
  * the target with "search_query: "
  * each candidate with "search_document: "
Without these, all embeddings cluster around 0.65-0.75 cosine and
the filter can't discriminate between matches and non-matches.

Public entry point:
    filter_comps_by_similarity(target, candidates, *, threshold)
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

DEFAULT_EMBED_MODEL = os.environ.get(
    "OLLAMA_EMBED_MODEL", "nomic-embed-text",
)
DEFAULT_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# Cosine-similarity threshold — set permissively (0.55).
#
# Lesson learned: small embedding models like nomic-embed-text weight
# COMMON English words ("electric scooter", "iPhone") heavily and
# brand-specific tokens (KUGOO, Razor) lightly. Empirically:
#   "KUGOO G2 MAX electric scooter" vs "KUGOO G2 PRO":   ~0.53
#   "KUGOO G2 MAX electric scooter" vs "Niu electric scooter": ~0.81
# That's the opposite of what we'd want. So we use embeddings as a
# SOFT filter — drop obvious junk (iPhone vs scooter at ~0.45) — and
# rely on the FB category filter as the primary "is this comparable?"
# signal. The category filter is exact and free from FB's own taxonomy.
DEFAULT_SIMILARITY_THRESHOLD = 0.55

# Lower bound on filtered comp set size. If fewer than this many
# comps survive the similarity filter, the appraiser should flag the
# listing as unscoreable (no reliable peer set).
MIN_FILTERED_COMPS = 3


@dataclass
class FilterResult:
    """Outcome of running the similarity filter on a comp set."""
    kept: list[int]              # indices into the original candidate list
    similarities: list[float]    # one per original candidate
    threshold: float
    target_text: str
    sufficient: bool             # True iff len(kept) >= MIN_FILTERED_COMPS


# Embedding model is small (137M params) and fast on CPU. We FORCE
# CPU-only inference (num_gpu=0) because keeping it on the GPU
# competes with the appraisal model for VRAM on tight-memory machines
# (e.g. 4GB GPUs) and causes timeouts when both want to load. CPU
# inference is ~0.5-1s per batch — fine.
DEFAULT_KEEP_ALIVE = "60s"
FORCE_CPU = True


def get_embeddings_batch(
    texts: list[str],
    *,
    model: str = DEFAULT_EMBED_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: int = 120,
    keep_alive: str = DEFAULT_KEEP_ALIVE,
) -> list[list[float] | None]:
    """Get embeddings for many strings in ONE batch call.

    Uses Ollama's `/api/embed` endpoint (newer, supports batch input)
    with `num_gpu: 0` to force CPU inference — keeps VRAM free for the
    appraisal LLM on small-GPU systems.

    Returns a list of vectors aligned with `texts`. On total failure
    every entry is None — caller should detect this and fall back to
    no filtering.
    """
    cleaned = [(t or "").strip() for t in texts]
    non_empty_idx = [i for i, t in enumerate(cleaned) if t]
    if not non_empty_idx:
        return [None] * len(texts)

    inputs = [cleaned[i] for i in non_empty_idx]
    payload = {
        "model": model,
        "input": inputs,
        "keep_alive": keep_alive,
    }
    if FORCE_CPU:
        payload["options"] = {"num_gpu": 0}
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/embed",
            json=payload,
            timeout=timeout_s,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as e:
        logger.warning("embed batch failed: %s", e)
        return [None] * len(texts)

    vecs = body.get("embeddings")
    if not isinstance(vecs, list) or len(vecs) != len(inputs):
        logger.warning(
            "embed batch returned bad shape: got %d vectors for %d inputs",
            len(vecs) if isinstance(vecs, list) else 0, len(inputs),
        )
        return [None] * len(texts)

    out: list[list[float] | None] = [None] * len(texts)
    for src_idx, vec in zip(non_empty_idx, vecs):
        if isinstance(vec, list) and vec:
            out[src_idx] = [float(x) for x in vec]
    return out


def get_embedding(
    text: str,
    *,
    model: str = DEFAULT_EMBED_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: int = 120,
) -> list[float] | None:
    """Single-text convenience wrapper around get_embeddings_batch."""
    result = get_embeddings_batch(
        [text], model=model, base_url=base_url, timeout_s=timeout_s,
    )
    return result[0] if result else None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]. Returns 0.0 on degenerate input
    (zero-magnitude vectors or mismatched dims) so callers don't have
    to special-case."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def filter_comps_by_similarity(
    target_text: str,
    candidate_texts: list[str],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    model: str = DEFAULT_EMBED_MODEL,
) -> FilterResult:
    """Score each candidate's similarity to `target_text`. Return a
    FilterResult with the indices that meet the threshold.

    Applies nomic-specific prefixes:
      * "search_query: <target>" for the listing being scored
      * "search_document: <candidate>" for each comp title
    Without these prefixes nomic returns muddy ~0.65-0.75 similarities
    for everything; with them, accessories drop to 0.55-0.65 and exact
    matches climb to 0.85+.

    On embedding failure (Ollama down, model missing) returns
    `sufficient=False, kept=[]` so callers know to skip filtering.
    """
    if not candidate_texts:
        return FilterResult(
            kept=[], similarities=[], threshold=threshold,
            target_text=target_text, sufficient=False,
        )

    prefixed = [f"search_query: {target_text}"]
    prefixed.extend(f"search_document: {t}" for t in candidate_texts)
    vecs = get_embeddings_batch(prefixed, model=model)

    target_vec = vecs[0]
    if target_vec is None:
        logger.warning(
            "no target embedding (Ollama down?); skipping similarity filter",
        )
        return FilterResult(
            kept=[],
            similarities=[0.0] * len(candidate_texts),
            threshold=threshold,
            target_text=target_text,
            sufficient=False,
        )

    sims: list[float] = []
    kept: list[int] = []
    for i, vec in enumerate(vecs[1:]):
        if vec is None:
            sims.append(0.0)
            continue
        s = cosine_similarity(target_vec, vec)
        sims.append(s)
        if s >= threshold:
            kept.append(i)

    return FilterResult(
        kept=kept,
        similarities=sims,
        threshold=threshold,
        target_text=target_text,
        sufficient=len(kept) >= MIN_FILTERED_COMPS,
    )
