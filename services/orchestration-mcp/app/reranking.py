"""FR-25: cross-encoder reranking pass over the top-N hybrid candidates,
calling the already-standalone reranker-service rather than loading the
model in-process. Degrades to the fused (pre-rerank) order on a reranker
outage rather than failing the whole query -- reranking improves ranking
quality, it isn't the thing that keeps unauthorized content out (that's the
access filter applied before any of this), so it's reasonable to keep serving
degraded-but-authorized results rather than a hard failure.

issue #89: an optional per-content-type score multiplier, applied to the
cross-encoder scores before sorting. Every chunk now carries a `content_type`
("text" or "table", set by ingestion-worker -- see app/chunking.py and
app/parsing.py) in its Qdrant payload; this is the "lighter-weight" half of
RAG-Anything's modality-aware ranking idea (arXiv 2510.12323) the project's
own REQUIREMENTS.md Section 11 deferred the full version of -- a
configurable weight per content type, not a dedicated multimodal retrieval
path. Defaults to no boost (every type weighted 1.0): there's no evidence
yet from the FR-30/FR-32 golden-query harness that a specific weighting
helps, so this wires up the mechanism for that harness to tune rather than
guessing a value.

Issue #419: RERANKER_URL need not point at this chart's own reranker-service
at all -- RERANKER_API_COMPATIBILITY selects "internal" (default, unchanged),
"tei" (HuggingFace text-embeddings-inference's native /rerank), or "cohere"
(the Jina/Cohere-style /v1/rerank convention, also what vLLM's own rerank
endpoints speak). See the RERANKER_API_COMPATIBILITY constant below for the
three wire shapes.
"""

from __future__ import annotations

import json
import logging
import math
import os

import httpx

from app import metrics
from common.log_safety import log_safe

logger = logging.getLogger(__name__)

RERANKER_URL = os.environ.get("RERANKER_URL", "http://reranker-service:8003")
# Issue #216: matches reranker-service's own RERANKER_SHARED_SECRET (app/main.py) --
# same value on both sides, out of band. Empty by default so this stays a no-op
# against a reranker-service that also hasn't set one (today's posture).
RERANKER_SHARED_SECRET = os.environ.get("RERANKER_SHARED_SECRET", "")

# Issue #419: which wire protocol RERANKER_URL above speaks.
# - "internal" (default): this chart's own reranker-service, unchanged --
#   POST {query, chunks: [{id, text}]} -> [{id, score}], authenticated (if
#   at all) with RERANKER_SHARED_SECRET above.
# - "tei": HuggingFace text-embeddings-inference's native /rerank --
#   POST {query, texts: [...]} -> [{index, score}, ...]. Recommended default
#   for a genuinely external endpoint (see the issue's decision writeup).
# - "cohere": the Jina/Cohere-style /v1/rerank convention -- POST {model,
#   query, documents: [...], top_n} -> {results: [{index, relevance_score}]}.
#   Also what vLLM's own /rerank, /v1/rerank, /v2/rerank endpoints speak --
#   vLLM does NOT speak the "tei" shape above despite also hosting
#   cross-encoder rerankers, so this is the mode to use against vLLM.
# "tei"/"cohere" authenticate with RERANKER_API_KEY as a bearer token
# instead of RERANKER_SHARED_SECRET, matching common/embedding_client.py's
# and common/completion_client.py's external-mode convention.
RERANKER_API_COMPATIBILITY = os.environ.get("RERANKER_API_COMPATIBILITY", "internal")
RERANKER_API_KEY = os.environ.get("RERANKER_API_KEY", "")
# Only meaningful in "cohere" mode, which requires a model field; "tei" has
# no such field and "internal" carries no model identity of its own (the
# reranker-service process already knows its own RERANKER_MODEL).
RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "")


def _load_content_type_boosts() -> dict[str, float]:
    raw = os.environ.get("CONTENT_TYPE_BOOSTS")
    if not raw:
        return {}
    try:
        return {str(k): float(v) for k, v in json.loads(raw).items()}
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return {}


# Deployment-wide default, e.g. CONTENT_TYPE_BOOSTS='{"table": 1.15}' to
# nudge table chunks ahead of text chunks the cross-encoder scores as
# roughly equally relevant. Overridable per call via rerank()'s
# content_type_boosts argument (see rag_search.py's same-named parameter).
CONTENT_TYPE_BOOSTS = _load_content_type_boosts()


def collapse_adjacent_overlaps(ranked: list[dict], top_k: int) -> tuple[list[dict], int]:
    """Issue #395: fill top_k from `ranked` while collapsing same-document
    adjacent-index chunks to the better-ranked one.

    FR-4's chunk overlap (CHUNK_OVERLAP_RATIO) means adjacent chunks of one
    document share text by construction; when a query matches that shared
    region both chunks survive fusion and rerank near-identically, so two
    top_k slots can carry nearly the same sentences while a genuinely
    different chunk ranked just below gets nothing. Measured on this repo's
    own docs ingested as a dev corpus (82 documents, 1278 chunks, 12 probe
    queries): 5% of top_k=5 slots and 7.5% of top_k=10 slots carried an
    overlap-adjacent duplicate.

    Walks `ranked` best-first, dropping a candidate only when a chunk from
    the same document with |chunk_index| distance 1 is already kept -- so
    the better-scoring side of each pair survives and the freed slot
    backfills with the next-ranked candidate (rag_search already hands in a
    pool larger than top_k for exactly this kind of headroom). Chunks
    lacking document_id/chunk_index are never collapsed. Cross-document
    near-duplicates are explicitly out of scope (see #395; FR-7 supersession
    covers the intended-duplicate case).

    Returns (kept candidates, dropped count).
    """
    kept: list[dict] = []
    kept_keys: list[tuple[object, object]] = []
    dropped = 0
    for candidate in ranked:
        if len(kept) == top_k:
            break
        payload = candidate.get("payload") or {}
        doc = payload.get("document_id")
        idx = payload.get("chunk_index")
        if (
            doc is not None
            and isinstance(idx, int)
            and any(d == doc and isinstance(i, int) and abs(i - idx) == 1 for d, i in kept_keys)
        ):
            dropped += 1
            continue
        kept.append(candidate)
        kept_keys.append((doc, idx))
    return kept, dropped


def _load_score_floor() -> float | None:
    """Issue #394: RERANK_SCORE_FLOOR, the minimum (boosted) cross-encoder
    score a candidate needs to be returned at all. Unset/empty = no floor,
    today's behavior. Parsed defensively -- read at import by the retrieval
    service, so a typo must not take the service down; a rejected value is
    logged and the floor stays off rather than silently guessing.
    """
    raw = os.environ.get("RERANK_SCORE_FLOOR", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("RERANK_SCORE_FLOOR=%r is not a number; relevance floor disabled", raw)
        return None
    # float() happily parses "nan"/"inf"/"-inf" (review on #415): inf would
    # empty every query -- a full retrieval outage dressed as configuration --
    # and nan poisons every >= comparison. Neither is a floor; refuse loudly.
    if not math.isfinite(value):
        logger.warning(
            "RERANK_SCORE_FLOOR=%r is not a finite number; relevance floor disabled", raw
        )
        return None
    return value


# Issue #394: without a floor, every query returns its top_k least-bad
# matches with full confidence -- an unanswerable question hands the
# generation model plausible-looking, access-authorized, entirely irrelevant
# passages, which is the failure abstention_accuracy then has to detect
# downstream. The floor applies to the cross-encoder's (boosted) score, not
# the fused RRF score: RRF emits rank-based values that aren't comparable
# across queries, while the cross-encoder's roughly are (the issue's own
# reasoning). Measured on this stack's dev corpus to pick a starting point:
# answerable queries' best chunks scored -2.5 .. +8.9, unanswerable ones
# -11.3 .. -2.8, with gross off-topic queries all below -6 -- so -5.0 is a
# permissive floor that drops clearly-unrelated content while keeping a
# 2.5-point margin to the hardest real query measured. Default off: turning
# it on is a deployment decision, not something an upgrade should do silently
# (a floor can hide results a deployment currently relies on).
#
# The scale is the serving model's, not this code's: those numbers are raw
# cross-encoder logits from the internal reranker-service. A #419 external
# endpoint ("tei"/"cohere") typically returns a normalized 0..1 relevance
# score instead, where -5.0 is a silent no-op. Re-tune the floor against
# whatever RERANKER_URL actually serves -- the mechanism is scale-agnostic,
# the number is not.
RERANK_SCORE_FLOOR = _load_score_floor()


async def rerank(
    query: str,
    candidates: list[dict],
    top_k: int,
    *,
    content_type_boosts: dict[str, float] | None = None,
) -> tuple[list[dict], str]:
    """candidates: list of dicts each with at least "id" and a "payload" dict
    containing "text" and "content_type". Returns (reranked candidates
    truncated to top_k, status note).

    content_type_boosts: optional per-call override of CONTENT_TYPE_BOOSTS --
    a {content_type: multiplier} map applied to each candidate's
    cross-encoder score before sorting. A content type absent from the map
    gets a 1.0 (no-op) multiplier."""
    if not candidates:
        return [], "no candidates to rerank"

    boosts = CONTENT_TYPE_BOOSTS if content_type_boosts is None else content_type_boosts

    headers: dict[str, str] = {}
    if RERANKER_API_COMPATIBILITY in ("tei", "cohere"):
        if RERANKER_API_KEY:
            headers["Authorization"] = f"Bearer {RERANKER_API_KEY}"
    elif RERANKER_SHARED_SECRET:
        headers["X-Reranker-Shared-Secret"] = RERANKER_SHARED_SECRET

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if RERANKER_API_COMPATIBILITY == "tei":
                resp = await client.post(
                    f"{RERANKER_URL}/rerank",
                    json={
                        "query": query,
                        "texts": [c["payload"].get("text", "") for c in candidates],
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                scores = {candidates[row["index"]]["id"]: row["score"] for row in resp.json()}
            elif RERANKER_API_COMPATIBILITY == "cohere":
                resp = await client.post(
                    f"{RERANKER_URL}/v1/rerank",
                    json={
                        "model": RERANKER_MODEL,
                        "query": query,
                        "documents": [c["payload"].get("text", "") for c in candidates],
                        "top_n": len(candidates),
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                scores = {
                    candidates[row["index"]]["id"]: row["relevance_score"]
                    for row in resp.json()["results"]
                }
            else:
                resp = await client.post(
                    f"{RERANKER_URL}/rerank",
                    json={
                        "query": query,
                        "chunks": [
                            {"id": c["id"], "text": c["payload"].get("text", "")}
                            for c in candidates
                        ],
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                scores = {row["id"]: row["score"] for row in resp.json()}
    except httpx.HTTPError as exc:
        logger.warning("reranker-service unavailable: %s: %s", type(exc).__name__, log_safe(exc))
        # #395: the collapse applies to the degraded path too -- overlap
        # redundancy is a property of chunking, not of which ranker ordered
        # the candidates.
        kept, dropped = collapse_adjacent_overlaps(candidates, top_k)
        note = "reranking unavailable; using fused order"
        if dropped:
            note += f"; {dropped} overlap-adjacent duplicate(s) collapsed"
        return kept, note

    def _boosted_score(candidate: dict) -> float:
        base = scores.get(candidate["id"], float("-inf"))
        if base == float("-inf"):
            return base
        weight = boosts.get(candidate["payload"].get("content_type", "text"), 1.0)
        return base * weight

    ranked = sorted(candidates, key=_boosted_score, reverse=True)
    # #394 before #395, deliberately: the floor judges relevance, the collapse
    # judges redundancy among what remains. Filtering first means an
    # irrelevant chunk can't survive by out-scoring its overlap neighbour,
    # and the collapse's backfill only draws from candidates that cleared the
    # floor.
    below_floor = 0
    if RERANK_SCORE_FLOOR is not None:
        surviving = [c for c in ranked if _boosted_score(c) >= RERANK_SCORE_FLOOR]
        below_floor = len(ranked) - len(surviving)
        ranked = surviving
        if below_floor:
            metrics.below_relevance_floor_total.inc(below_floor)
    # #395: collapse over the full ranked pool, not a pre-truncated slice --
    # the freed slots backfill with the next-ranked distinct candidates.
    kept, collapsed = collapse_adjacent_overlaps(ranked, top_k)
    note = "cross-encoder reranking applied"
    if boosts:
        note += f", content-type boosts applied {boosts}"
    if below_floor:
        # #394: surfacing "no good match" beats silently thinning results --
        # and when everything drops, the caller's empty-results path already
        # tells the model to say no approved document was found.
        note += f"; {below_floor} candidate(s) below the relevance floor ({RERANK_SCORE_FLOOR})"
    if collapsed:
        # The issue's other complaint was that the duplication "is not
        # visible in the response" -- so its removal is.
        note += f"; {collapsed} overlap-adjacent duplicate(s) collapsed"
    return kept, note
