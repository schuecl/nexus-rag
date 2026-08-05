"""Issue #411: seed_sample_data must be idempotent -- eval-retrieval's
depends_on re-triggers it on every `compose --profile eval run`, so a re-run
must leave the corpus unchanged instead of growing it by 7. These tests pin
plan_action(), the pure decision that makes that true; the API-driven flow
around it is exercised against the live stack (see the PR's validation)."""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/ ships no package; put it on the path the same way the harness runs.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from seed_sample_data import plan_action


def _doc(filename: str, status: str, id_: str = "id-1") -> dict:
    return {"id": id_, "filename": filename, "status": status}


class TestPlanAction:
    def test_no_prior_copy_submits(self):
        assert plan_action([], "public-notice.md", "approved") == ("submit", {})

    def test_terminal_copy_skips_and_returns_it(self):
        doc = _doc("public-notice.md", "approved")

        assert plan_action([doc], "public-notice.md", "approved") == ("skip", doc)

    def test_pending_copy_resumes_curation(self):
        doc = _doc("outdated-vpn-guide.md", "pending_review")

        assert plan_action([doc], "outdated-vpn-guide.md", "rejected") == ("curate", doc)

    def test_pending_review_as_the_terminal_state_skips(self):
        # draft-travel-policy.md's *intended* end state is pending_review, so
        # a pending copy is complete, not half-seeded.
        doc = _doc("draft-travel-policy.md", "pending_review")

        assert plan_action([doc], "draft-travel-policy.md", "pending_review") == ("skip", doc)

    def test_terminal_wins_over_pending_when_both_exist(self):
        # A crashed run can leave both a pending and (from an earlier run) a
        # terminal copy; the terminal copy makes a re-run a no-op.
        pending = _doc("password-policy.md", "pending_review", "id-pending")
        done = _doc("password-policy.md", "approved", "id-done")

        action, doc = plan_action([pending, done], "password-policy.md", "approved")

        assert (action, doc["id"]) == ("skip", "id-done")

    def test_failed_or_rejected_copies_do_not_block_resubmission(self):
        # Matches ingest_repo_docs.py's convention.
        existing = [
            _doc("public-notice.md", "failed"),
            _doc("public-notice.md", "rejected"),
        ]

        assert plan_action(existing, "public-notice.md", "approved") == ("submit", {})

    def test_other_filenames_are_ignored(self):
        existing = [_doc("password-policy.md", "approved")]

        assert plan_action(existing, "public-notice.md", "approved") == ("submit", {})

    def test_superseded_v1_judged_by_its_own_terminal_status(self):
        # The supersession pair is planned with terminal_status="approved" for
        # v1 only while deciding whether to (re)create it as a supersession
        # target -- an already-superseded v1 is not "approved", so the caller
        # judges the pair by v2 instead (see main()); this pins that a
        # superseded copy alone does not read as reusable.
        superseded = _doc("network-access-sop-v1.md", "superseded")

        assert plan_action([superseded], "network-access-sop-v1.md", "approved") == ("submit", {})
