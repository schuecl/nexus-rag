# TEMPLATE — management review minutes

> **This file is a template, not a record.** Copy it to
> `evidence/snapshots/<YYYY-MM-DD>/management-review.md` and fill it in during or
> immediately after the review. Do not fill it in speculatively: a plausible-looking
> set of minutes for a meeting that did not happen is a fabricated audit record,
> which is materially worse for this evidence package than the honest gap it would
> paper over. Leave every field you cannot answer blank.

Serves GOVERN 1.5 (periodic review of the risk-management process), GOVERN 4.1 /
MANAGE 4.2 (evidenced continual improvement). Process and cadence:
[governance-policy.md](../../governance-policy.md) §9.

## Meeting

| Field | Value |
|---|---|
| Date | |
| Cycle | e.g. Q3 2026 |
| Chair | |
| Attendees (name, role) | |
| Absent / not represented | |
| Previous review | link, or "none — this is the first" |

## Inputs reviewed

Tick what was actually in front of the meeting. An input nobody opened should be
left unticked — it changes how much weight the decisions below can carry.

- [ ] [Open-decisions ledger](../../README.md) (rows 1–13)
- [ ] [Risk register](../../risk-register.md), including accepted-risk rows
- [ ] [Evidence index](../evidence-index.md) known-gaps list
- [ ] [Waiver register](../waiver-register.md) — including any interim §3.3 waivers flagged for retroactive review
- [ ] Retrieval trend store (`.eval-history`) / calibration trend store
- [ ] Most recent internal-audit report (if any)
- [ ] Open security findings (`sec:*` / `severity:*` labels)

## Decisions

One row per ledger decision addressed. "Deferred" requires a date — a deferral
without one is how an open decision becomes a permanent one.

| Ledger row | Decision | Accept / Amend / Defer | If amended: what changed | If deferred: until | Recorded in |
|---|---|---|---|---|---|
| 1 — accountable AI owner appointment | | | | | governance-policy §2.1 |
| 2 — risk tolerance ratification | | | | | governance-policy §3 |
| 3 — retention periods, audit expiry, filename minimization | | | | | governance-policy §8 |
| 4 — incident response receiver, ladder, taxonomy | | | | | governance-policy §7 |
| 5 — identity governance, classified-gate owners | | | | | rmf-mapping GOVERN 1.1 |
| 6 — NFR-4 end-to-end latency budget (generation leg) | | | | | rmf-mapping MAP 1.6 |
| 7 — golden-query gate: formal acceptance or required check | | | | | risk-register R-6 |
| 8 — qwen2.5 defaults vs constraint C2 | | | | | ai-system-inventory A1 |
| 9 — Milvus maintainer-of-record vs C2 | | | | | ai-system-inventory A3 |
| 10 — corpus licensing field | | | | | ai-system-inventory §6 |
| 11 — impact-assessment Q1–Q5 | | | | | impact-assessment §7 |
| 12 — review cadence + first review | | | | | governance-policy §9.2 |
| 13 — curator risk-awareness training ownership | | | | | governance-policy §10 |

## Cadence ratification (closes ledger row 12, acceptance criterion 1)

Copy the filled values into governance-policy §9.2's record block — this table is
the minute, that block is the standing policy, and both need to agree.

| Field | Value |
|---|---|
| Management-review cadence | |
| Internal-audit cadence | |
| Internal audit performed by (§9.3) | |
| Independence basis (MEASURE 1.3) | |

## Risk-management process itself (GOVERN 1.5)

Not the risks — the *process*. What is asked here is whether the machinery is
working, which is the outcome this review exists to evidence.

- Did any gate fail-open, get waived, or get bypassed this cycle?
- Did any risk materialize that the register did not anticipate?
- Are the gate/waiver mechanisms proportionate, or are they being routed around?
- Changes to the risk register agreed (rows added, re-scored, accepted, closed):

## Actions

| # | Action | Owner | Due | Issue |
|---|---|---|---|---|
| | | | | |

## Next review

| Field | Value |
|---|---|
| Scheduled for | |
| Standing items carried forward | |

## Sign-off

```
Chair:                    ______________________ (name, date)
Accountable AI owner:     ______________________ (name, date)
```
