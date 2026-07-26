"""Issue #72: runtime observability for the retrieval path.

Two surfaces, deliberately different in what they carry:

- **Per-request timings** go into the FR-31 audit entry, where they sit next to
  the actor and the authorization outcome. Useful for answering "why was *this*
  query slow" and for the retrieval-pattern anomaly detection #127 wants.
- **Aggregates** go here, for scraping. Useful for answering "is retrieval
  slow" and for ever putting a number on NFR-4's latency budget, which
  REQUIREMENTS.md still lists as an open question.

Timings are deliberately **not** returned to the caller. Response latency is a
side channel: it correlates with how much the access filter matched and how
many candidates the reranker scored, so handing a caller precise per-stage
numbers gives membership inference a cleaner signal than the wall-clock it can
already measure. Operators get them via the audit log; callers do not.

Label cardinality is kept deliberately low and content-free -- stage names and
outcome names only. No user, no query, no document id ever becomes a label:
Prometheus metrics are typically far more widely readable than the corpus, and
a per-user label would rebuild exactly the surveillance surface #125 removed
from the audit log.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# Buckets chosen for this pipeline rather than the library default: a dense
# embedding call plus a cross-encoder rerank on CPU lands in the hundreds of
# milliseconds to low seconds, so the default buckets (which top out at 10s)
# waste resolution at the low end where the interesting variance is.
_LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

query_stage_seconds = Histogram(
    "nexus_rag_query_stage_seconds",
    "Wall-clock duration of one stage of a rag_search call.",
    ["stage"],
    buckets=_LATENCY_BUCKETS,
)

queries_total = Counter(
    "nexus_rag_queries_total",
    "rag_search calls by outcome.",
    ["outcome"],
)

reranker_fallback_total = Counter(
    "nexus_rag_reranker_fallback_total",
    "Queries served in fused (pre-rerank) order because reranker-service was "
    "unreachable. FR-25 degrades rather than failing, so this is otherwise "
    "invisible -- a rising rate means ranking quality has quietly dropped.",
)

results_returned = Histogram(
    "nexus_rag_results_returned",
    "Number of chunks returned to the caller.",
    buckets=(0, 1, 2, 5, 10, 20, 50),
)


def render() -> tuple[bytes, str]:
    """The scrape payload and its content type."""
    return generate_latest(), CONTENT_TYPE_LATEST
