# SIEM detection runbook — reconnaissance-shaped query patterns

Issue [#436](https://github.com/schuecl/nexus-rag/issues/436). This document
translates the four signals `scripts/detect_query_anomalies.py` computes (#426)
into detection content an operator can build in whichever SIEM the deployment
actually runs, working from the audit rows #73's exporter already ships.

It is **documentation only**. Nothing here is executed or tested by this repo's
CI, and that is the reason it is a runbook rather than code:
`docs/threat-model.md`'s section 4 records SIEM rule content as deliberately
out of scope, because Splunk SPL, Elastic KQL/EQL, ArcSight and QRadar rule
formats are environment-specific and there is no way to exercise them here.
Treat every query below as a **sketch to adapt**, not a drop-in artifact — the
field names depend on how your collector parses the syslog MSG field, which this
repo cannot know.

## When you need this

You need it if detection should live in the SIEM rather than in (or in addition
to) periodic runs of `scripts/detect_query_anomalies.py`. The trade-off:

| | `detect_query_anomalies.py` | SIEM detection content |
| --- | --- | --- |
| Runs | manually, or on a schedule you supply (nothing in this repo schedules it) | continuously, by the SIEM |
| Reads | Postgres `audit_log` via the `nexus_rag_audit_reporting` SELECT-only role | the exported syslog stream |
| Attribution | `actor_sub`/`actor_username` in its stdout report only | whatever your SIEM's RBAC exposes |
| Correctness | unit-tested in `tests/unit/test_detect_query_anomalies.py`; validated against a live stack | your responsibility |

Running both is reasonable. They read the same fields, so they agree by
construction — which also makes the script a way to sanity-check a new SIEM rule
over the same window.

**One privacy constraint carries over and must not be relaxed in the SIEM.**
Per-identity detail is intentionally confined: #125 removed query *text* from the
audit log entirely, and `orchestration-mcp/app/metrics.py` refuses a per-user
metric label because it "would rebuild exactly the surveillance surface #125
removed." A SIEM rule keying on `actor_sub` is fine — that is what the audit log
is for, and `docs/governance.md` already names the audience holding audit-read
authority. Building a per-user *dashboard* of query behaviour for a wider
audience is the thing to avoid.

## The wire format your rule has to parse

`services/common/common/siem.py::format_rfc5424` emits one RFC 5424 message per
audit row: a syslog header carrying the routing/triage fields, then the whole
event as JSON in the MSG field.

```
<109>1 2026-08-06T04:12:33.914217Z rag-host nexus-rag-orchestration-mcp 1 query - {"id": "...", "service": "orchestration-mcp", "actor_sub": "...", "actor_username": "bob-query", "action": "query", "target_id": null, "detail": {"top_k": 5, "result_count": 3, ...}, "created_at": "2026-08-06T04:12:33.914217Z"}
```

| Position | Value | Notes |
| --- | --- | --- |
| PRI | `109` normal, `108` for denials | facility 13 (`log_audit`) × 8 + severity. NOTICE = 5 → 109; WARNING = 4 → 108. `SIEM_SYSLOG_FACILITY` can override the facility, so **key on MSGID, not PRI**, if the deployment sets it |
| VERSION | `1` | |
| TIMESTAMP | RFC 3339, UTC, `Z` suffix | same value as `created_at` in the JSON |
| HOSTNAME | collector-visible host | |
| APP-NAME | `nexus-rag-<service>` | e.g. `nexus-rag-orchestration-mcp` |
| PROCID | pid | |
| MSGID | the audit action | `query`, `query.denied`, `document.submit`, … — the field to filter on |
| STRUCTURED-DATA | `-` (nil) | nothing is carried here |
| MSG | the full event as JSON | ASCII-escaped (`ensure_ascii=True`), so control characters cannot forge record boundaries |

Two things worth knowing before you write a parser:

- **Denials arrive at WARNING, everything else at NOTICE.** That is severity
  *only* — `_severity()` keys on the substring `denied` in the action. It is a
  triage convenience, not a security boundary.
- **There is no query text to match on, by design.** Any rule attempting
  near-duplicate *text* correlation has nothing to work with; `result_count` is
  the substitute (see `narrow_probe_shaped` below). This is why signal 3 is
  shaped the way it is rather than the way #426 originally suggested.

Fields the rules below need, all inside the MSG JSON: `actor_sub`, `action`,
`detail.result_count`, `created_at`.

## The four signals

Thresholds are the script's defaults (`scripts/detect_query_anomalies.py:98-105`).
They are starting points calibrated against a dev corpus, not agreed operational
values — expect to tune them against your own traffic before enabling alerting.

Scope for every signal: **`query` and `query.denied` actions only.**
`query.failed` is excluded deliberately — it is the #122 embedding-mismatch
guard, an operational condition rather than a user action, and including it
would let an infrastructure fault look like reconnaissance.

### 1. `high_volume` — attempt-rate spike

Total attempts (`query` + `query.denied`) per identity at or above
**30 in 60 minutes**. Catches naive scripted probing.

**Splunk SPL**

```spl
index=nexus_rag (MSGID="query" OR MSGID="query.denied")
| spath input=_raw output=actor_sub path=actor_sub
| bin _time span=60m
| stats count AS attempts by actor_sub, _time
| where attempts >= 30
```

**Elastic (ES|QL)**

```esql
FROM nexus-rag-audit-*
| WHERE action IN ("query", "query.denied")
| STATS attempts = COUNT(*) BY actor_sub, bucket = BUCKET(@timestamp, 1 hour)
| WHERE attempts >= 30
```

### 2. `high_denial_ratio` — one identity's denial *rate*

`query.denied` as a share of that identity's attempts, at or above **0.3**,
gated by a minimum of **10 attempts** so one denial from a brand-new user isn't
100 % of one.

Distinct from the existing `NexusRagQueryDeniedSpike` Prometheus alert
(`infra/observability/prometheus/rules/nexus-rag.yml`), which fires on denial
*volume across all identities*. A single identity being denied steadily can stay
under that aggregate threshold indefinitely — this is the rule that catches it.

**Splunk SPL**

```spl
index=nexus_rag (MSGID="query" OR MSGID="query.denied")
| spath input=_raw output=actor_sub path=actor_sub
| bin _time span=60m
| stats count AS attempts, count(eval(MSGID="query.denied")) AS denials
        by actor_sub, _time
| where attempts >= 10 AND (denials / attempts) >= 0.3
```

**Elastic (ES|QL)**

```esql
FROM nexus-rag-audit-*
| WHERE action IN ("query", "query.denied")
| STATS attempts = COUNT(*),
        denials = COUNT(CASE(action == "query.denied", 1, NULL))
    BY actor_sub, bucket = BUCKET(@timestamp, 1 hour)
| WHERE attempts >= 10 AND denials::double / attempts::double >= 0.3
```

### 3. `narrow_probe_shaped` — successful queries resolving to 0–1 chunks

Share of that identity's **successful** queries whose `detail.result_count` is
0 or 1, at or above **0.6**, gated by a minimum of **10 successes**.

This is the signal that replaces "near-duplicate query text", which is
unbuildable here: #125 means the text was never stored. A user running many
queries that each resolve to at most one chunk is the membership-inference shape
OWASP describes — crafted, narrow questions checking one document at a time —
and `result_count` carries that without any content.

Note the direction: an **absent** result is informative to the prober, so a run
of zero-result successes is as interesting as single-result ones. Filter on
successful `query` rows only; denials have no meaningful `result_count`.

**Splunk SPL**

```spl
index=nexus_rag MSGID="query"
| spath input=_raw output=actor_sub path=actor_sub
| spath input=_raw output=result_count path=detail.result_count
| bin _time span=60m
| stats count AS successes,
        count(eval(result_count <= 1)) AS narrow
        by actor_sub, _time
| where successes >= 10 AND (narrow / successes) >= 0.6
```

**Elastic (ES|QL)**

```esql
FROM nexus-rag-audit-*
| WHERE action == "query"
| STATS successes = COUNT(*),
        narrow = COUNT(CASE(detail.result_count <= 1, 1, NULL))
    BY actor_sub, bucket = BUCKET(@timestamp, 1 hour)
| WHERE successes >= 10 AND narrow::double / successes::double >= 0.6
```

### 4. `boundary_mapping` — denial immediately followed by success

Count of `query.denied` → `query` transitions for the same identity within
**300 seconds**, at or above **5** occurrences.

**Read the name with care, because it does not mean what it sounds like.** The
script's docstring records what a live run established: `rag_search.py`'s only
`query.denied` path is the coarse missing-`rag-query`-role gate
(`if not claims.can_query`) — **not** a per-query FR-26
classification/releasability/access-scope mismatch. An out-of-scope query returns
a *successful empty result*, never a denial. So this signal does not detect
someone mapping where an access filter's edge sits. What it actually detects is
an identity's `rag-query` grant **changing state mid-window and being used
immediately after**: a role revoked then reinstated, or a delayed token refresh
picking up a just-granted role. Rarer, narrower, still worth a human look — and
worth writing that into your alert's description so a responder isn't hunting for
filter probing that the signal cannot see.

Because this is a sequence rather than an aggregate, it is the one signal whose
shape differs most per SIEM. Splunk's `transaction`/`streamstats` and Elastic's
EQL `sequence` are the natural primitives:

**Splunk SPL** (`streamstats` to find the previous action per identity)

```spl
index=nexus_rag (MSGID="query" OR MSGID="query.denied")
| spath input=_raw output=actor_sub path=actor_sub
| sort 0 actor_sub, _time
| streamstats current=f last(MSGID) AS prev_action, last(_time) AS prev_time
              by actor_sub
| where MSGID="query" AND prev_action="query.denied"
        AND (_time - prev_time) <= 300
| stats count AS transitions by actor_sub
| where transitions >= 5
```

**Elastic (EQL sequence)**

```eql
sequence by actor_sub with maxspan=5m
  [ any where action == "query.denied" ]
  [ any where action == "query" ]
```

EQL reports each matching sequence; add a rule threshold of 5 per identity per
hour to match the script.

## Verifying a rule you just wrote

Run the in-repo detector over the same window and compare which identities each
flags:

```bash
python scripts/detect_query_anomalies.py --lookback-minutes 60

# Or against the dev stack's own Postgres, via the compose one-shot:
docker compose --profile anomaly-detection run --rm detect-query-anomalies
```

The script prints `actor_sub`/`actor_username` per flagged signal. If a SIEM rule
and the script disagree over the same window, the usual causes are: the rule
including `query.failed`; the rule counting denials in the `narrow_probe_shaped`
denominator; or a bucket boundary splitting a burst across two windows where the
script uses a single rolling lookback.

## What this runbook does not give you

- **Tuned thresholds.** The defaults above are dev-corpus starting points. A
  deployment with a large corpus and heavy legitimate use will need higher
  volume thresholds, and a deployment where most queries legitimately return one
  chunk will need `narrow_probe_shaped` reconsidered entirely.
- **Response procedure.** What to do with a flagged identity — revoke
  `rag-query`, contact the user, escalate — is a deployment policy decision, not
  something this repo can specify.
- **Prevention.** These are detections. `docs/threat-model.md` section 1 covers
  the controls that narrow the membership-inference channel itself (score
  suppression per #127, per-classification collections); this section 4 material
  exists because that channel cannot be fully closed against an authorized
  adversary.

## Related

- `scripts/detect_query_anomalies.py` — the in-repo equivalent; its module
  docstring is the authoritative signal definition and the source for this page
- [#426](https://github.com/schuecl/nexus-rag/issues/426) — implements the
  detector; [#127](https://github.com/schuecl/nexus-rag/issues/127) gap #4 — the
  threat it addresses
- [#73](https://github.com/schuecl/nexus-rag/issues/73) — the RFC 5424 export
  this runbook builds on (`services/common/common/siem.py`)
- `docs/threat-model.md` section 4 — records SIEM rule content as a deliberate
  residual, which this page closes as documentation
- `docs/governance.md` — who holds audit-read authority
- `docs/observability.md` — metrics/alerting, including
  `NexusRagQueryAnomalyDetected` and `NexusRagQueryDeniedSpike`
