# Evidence snapshots

One directory per governance cycle, named `YYYY-MM-DD` after the date of the
review or audit it holds. This is where
[governance-policy.md](../../governance-policy.md) §9's evidence accumulates.

**Currently empty of cycles — no management review or internal audit has been
conducted.** That is the honest state, and it is the gap issue #542 exists to
close. The mechanism, the template, and the baseline are all in place; what is
missing is a review actually being held and signed.

## What goes in a cycle directory

| File | Produced by | Contents |
|---|---|---|
| `management-review.md` | the review itself, from [`management-review-template.md`](management-review-template.md) | attendees, decisions taken against [ledger](../../README.md) row numbers, deferrals with dates |
| `internal-audit.md` | `scripts/audit_rmf_mapping.py --report`, then completed by hand | mechanical findings, the diff since the last audit, the auditor's judgement, signatures |

## `baseline.json`

The machine-readable snapshot of [rmf-mapping.md](../../rmf-mapping.md)'s 45 rows
— statuses, cited file references, and referenced issues — that the next internal
audit diffs against. It records the commit it was generated from, so two auditors
running the audit on the same commit produce the same artifact.

Refresh it only when an audit has been completed and its findings accepted:

```
python scripts/audit_rmf_mapping.py --snapshot docs/nist-ai-rmf/evidence/snapshots/baseline.json
```

Refreshing it at any other time destroys the comparison the next audit depends on
— the diff is against "the state at the last accepted audit", not "the state this
morning".

## Why files rather than a wiki or a calendar invite

An auditor reads artifacts. A meeting that happened without minutes landing here
cannot be evidenced afterwards, which is the specific reason this item is the one
the [evidence index](../evidence-index.md) reports as a gap while thirteen others
have drafts: the others could be written; this one can only be accumulated.
