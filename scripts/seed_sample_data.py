"""NFR-9: pre-seed the dev stack with sample documents spanning a range of
Classification/Releasability/Access-scope/Status values, using the seeded
Keycloak realm's test users (infra/keycloak/realm-export), so a fresh
clone-and-run has real data to exercise RBAC scenarios against (allowed
query, denied query, pending vs. approved, curator approve/reject) without
manual setup.

Runs after ingestion-api and Keycloak are healthy -- see the
seed-sample-data service in docker-compose.yml. Idempotent since issue #411:
a document whose filename already sits in its intended terminal status (from
a prior seed run, listed via /documents/mine per uploader) is skipped, and a
half-seeded document (submitted but never curated -- a crashed prior run) has
just its curation step resumed. Re-running therefore leaves the corpus
unchanged instead of growing it by 7. That stopped being an acceptable
simplification when eval-retrieval's depends_on made every
`docker compose --profile eval run` re-trigger this script: each local eval
invocation silently grew the corpus, so eval metrics drifted with how many
times you'd run them (fine in CI's fresh volumes, wrong everywhere else).

The supersession pair needs the one nuanced check: v1 is *supposed* to end
superseded, so the pair is judged by v2 -- an approved v2 means the whole
FR-7 flow already ran, while an approved-but-not-superseded v1 without a v2
(a crash between the two submissions) resumes by submitting v2 against the
existing v1.
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx
from _keycloak import KEYCLOAK_URL, REALM, get_token, wait_until_up

INGESTION_API_URL = os.environ.get("INGESTION_API_URL", "http://ingestion-api:8001")

READY_TIMEOUT_SECONDS = 120
PROCESSING_TIMEOUT_SECONDS = 60


def wait_until_ready() -> None:
    wait_until_up(
        [
            f"{INGESTION_API_URL}/health",
            f"{KEYCLOAK_URL}/realms/{REALM}/.well-known/openid-configuration",
        ],
        timeout_seconds=READY_TIMEOUT_SECONDS,
    )


def submit(
    token: str,
    filename: str,
    text: str,
    *,
    classification: str,
    releasability: list[str],
    access_scope: list[str],
    doc_type: str = "SOP",
    source_originator: str = "Sample Data",
    supersedes: str | None = None,
) -> dict:
    data = {
        "classification": classification,
        "releasability": json.dumps(releasability),
        "access_scope": json.dumps(access_scope),
        "source_originator": source_originator,
        "doc_type": doc_type,
    }
    if supersedes:
        data["supersedes_document_id"] = supersedes
    resp = httpx.post(
        f"{INGESTION_API_URL}/documents",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (filename, text.encode(), "text/markdown")},
        data=data,
        timeout=60,
    )
    resp.raise_for_status()
    doc = resp.json()
    # FR-8: submission is accepted (202) before parse/chunk/embed has
    # actually run; curation (approve/reject, called right after this in
    # main()) requires the document to already be `pending_review`, so wait
    # for the background pipeline to reach a terminal state here rather than
    # racing it.
    return wait_for_processed(token, doc["id"])


def wait_for_processed(token: str, doc_id: str) -> dict:
    deadline = time.monotonic() + PROCESSING_TIMEOUT_SECONDS
    doc = None
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{INGESTION_API_URL}/documents/{doc_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        doc = resp.json()
        if doc["status"] not in ("queued", "processing"):
            return doc
        time.sleep(1)
    raise RuntimeError(
        f"document {doc_id} did not finish processing within "
        f"{PROCESSING_TIMEOUT_SECONDS}s (last status: {doc['status'] if doc else 'unknown'})"
    )


def approve(token: str, doc_id: str) -> dict:
    resp = httpx.post(
        f"{INGESTION_API_URL}/curate/{doc_id}/approve",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def reject(token: str, doc_id: str, reason: str) -> dict:
    resp = httpx.post(
        f"{INGESTION_API_URL}/curate/{doc_id}/reject",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": reason},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def list_mine(token: str) -> list[dict]:
    resp = httpx.get(
        f"{INGESTION_API_URL}/documents/mine",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def plan_action(existing: list[dict], filename: str, terminal_status: str) -> tuple[str, dict]:
    """Issue #411: what a re-run should do about one seed document.

    Returns ("skip", doc) when a prior run already left `filename` in its
    intended terminal status, ("curate", doc) when a prior run submitted it
    but crashed before the curation step (still pending_review), and
    ("submit", {}) when no usable copy exists. Failed and unexpectedly-
    rejected copies don't block a fresh submission, matching
    ingest_repo_docs.py's convention.
    """
    matches = [d for d in existing if d.get("filename") == filename]
    for doc in matches:
        if doc.get("status") == terminal_status:
            return "skip", doc
    for doc in matches:
        if doc.get("status") == "pending_review":
            return "curate", doc
    return "submit", {}


def ensure(
    uploader_token: str,
    filename: str,
    terminal_status: str,
    submit_fn,
    curate_fn,
) -> tuple[dict, str]:
    """Bring `filename` to `terminal_status`, reusing prior-run state.

    submit_fn() submits and waits for processing; curate_fn(doc) applies the
    curation step (or does nothing for a document meant to stay
    pending_review). Returns (document, how) with how in skip|curate|submit.
    """
    action, doc = plan_action(list_mine(uploader_token), filename, terminal_status)
    if action == "skip":
        print(f"SKIP     {filename} (already {terminal_status} from a prior run)")
        return doc, action
    if action == "curate":
        print(f"RESUME   {filename} (pending_review from a prior run; applying curation)")
        return curate_fn(doc), action
    doc = submit_fn()
    return curate_fn(doc), action


def main() -> None:
    print("Waiting for ingestion-api and Keycloak...")
    wait_until_ready()

    alice = get_token("alice-ingest")  # rag-ingest, CUI
    carol = get_token("carol-curator")  # rag-query + rag-curate:USAREUR-AF, SECRET
    dave = get_token("dave-admin")  # all roles, SECRET

    seeded: list[tuple[str, str, str]] = []

    ensure(
        alice,
        "public-notice.md",
        "approved",
        lambda: submit(
            alice,
            "public-notice.md",
            "# All-Hands Notice\n\nThe cafeteria will be closed for renovations "
            "starting next month. Alternate dining options will be posted on "
            "the intranet.",
            classification="UNCLASSIFIED",
            releasability=["NONE"],
            access_scope=["ALL_AUTHENTICATED"],
            doc_type="Notice",
        ),
        lambda d: approve(carol, d["id"]),
    )
    seeded.append(("public-notice.md", "approved", "UNCLASSIFIED / NONE / ALL_AUTHENTICATED"))

    ensure(
        alice,
        "password-policy.md",
        "approved",
        lambda: submit(
            alice,
            "password-policy.md",
            "# Password Policy\n\nAll passwords must be rotated every 90 days and "
            "contain a mix of uppercase, lowercase, and numeric characters. "
            "Reused passwords from the last 12 rotations are rejected.",
            classification="CUI",
            releasability=["FVEY"],
            access_scope=["USAREUR-AF"],
        ),
        lambda d: approve(carol, d["id"]),
    )
    seeded.append(("password-policy.md", "approved", "CUI / USAREUR-AF"))

    ensure(
        alice,
        "draft-travel-policy.md",
        # pending_review IS this document's terminal state -- it exists to
        # exercise the "never approved, never retrievable" scenario, so its
        # curation step is a no-op.
        "pending_review",
        lambda: submit(
            alice,
            "draft-travel-policy.md",
            "# Draft Travel Policy\n\nThis document is still under review and "
            "covers TDY reimbursement procedures for temporary duty travel.",
            classification="CUI",
            releasability=["FVEY"],
            access_scope=["USAREUR-AF"],
        ),
        lambda d: d,
    )
    seeded.append(
        ("draft-travel-policy.md", "pending_review (left unreviewed)", "CUI / USAREUR-AF")
    )

    ensure(
        alice,
        "outdated-vpn-guide.md",
        "rejected",
        lambda: submit(
            alice,
            "outdated-vpn-guide.md",
            "# VPN Setup Guide (Draft)\n\nThis guide references a VPN client "
            "that has since been deprecated and should not be used.",
            classification="CUI",
            releasability=["FVEY"],
            access_scope=["USAREUR-AF"],
        ),
        lambda d: reject(
            carol, d["id"], "References a deprecated VPN client; needs rewrite before publication."
        ),
    )
    seeded.append(("outdated-vpn-guide.md", "rejected", "CUI / USAREUR-AF"))

    # Issue #277 (gap G1): access_scope is now a hard requirement to approve a
    # pending document, same as clearance/releasability -- carol-curator has
    # no Signal-Corps group membership, so she can no longer approve this one
    # (previously this was the gap: any USAREUR-AF curator could approve any
    # USAREUR-AF-owned document regardless of its access_scope). dave-admin is
    # provisioned with the Signal-Corps group and rag-curate:Signal-Corps
    # specifically so this sample document has an eligible approver.
    ensure(
        dave,
        "incident-response-plan.md",
        "approved",
        lambda: submit(
            dave,
            "incident-response-plan.md",
            "# Incident Response Plan\n\nUpon detection of a network intrusion, "
            "the Signal Corps duty officer must be notified within 15 minutes "
            "and the affected segment isolated.",
            classification="SECRET",
            releasability=["NATO", "FVEY"],  # demonstrates a multi-value Releasability
            access_scope=["Signal-Corps"],
        ),
        lambda d: approve(dave, d["id"]),
    )
    seeded.append(("incident-response-plan.md", "approved", "SECRET / NATO+FVEY / Signal-Corps"))

    # FR-7 supersession pair. v1's *intended* end state is superseded, so the
    # pair is judged by v2 (see module docstring): an approved v2 means the
    # whole flow already ran; otherwise the flow resumes from whatever half-
    # seeded state a crashed run left (approved v1 without a v2, or a
    # pending v2 already submitted with its supersedes link).
    alice_docs = list_mine(alice)
    v2_action, v2_doc = plan_action(alice_docs, "network-access-sop-v2.md", "approved")
    if v2_action == "skip":
        print("SKIP     network-access-sop-v1.md + v2 (supersession already ran in a prior run)")
    else:
        v1_action, d_v1 = plan_action(alice_docs, "network-access-sop-v1.md", "approved")
        if v1_action == "submit":
            d_v1 = submit(
                alice,
                "network-access-sop-v1.md",
                "# Network Access SOP (v1)\n\n"
                "VPN access requires a hardware token and manager approval.",
                classification="CUI",
                releasability=["FVEY"],
                access_scope=["USAREUR-AF"],
            )
            approve(carol, d_v1["id"])
        elif v1_action == "curate":
            print("RESUME   network-access-sop-v1.md (pending_review; approving)")
            approve(carol, d_v1["id"])
        else:
            print("RESUME   network-access-sop-v1.md (approved v1 from a prior run; adding v2)")
        if v2_action == "curate":
            # A pending v2 was already submitted with its supersedes link.
            print("RESUME   network-access-sop-v2.md (pending_review; approving)")
            approve(carol, v2_doc["id"])
        else:
            d_v2 = submit(
                alice,
                "network-access-sop-v2.md",
                "# Network Access SOP (v2)\n\nVPN access requires a hardware token, "
                "manager approval, and completion of the annual security awareness "
                "course.",
                classification="CUI",
                releasability=["FVEY"],
                access_scope=["USAREUR-AF"],
                supersedes=d_v1["id"],
            )
            approve(carol, d_v2["id"])
    seeded.append(("network-access-sop-v1.md", "superseded (by v2)", "CUI / USAREUR-AF"))
    seeded.append(("network-access-sop-v2.md", "approved (FR-7 new version)", "CUI / USAREUR-AF"))

    print(f"\nSeeded {len(seeded)} documents:")
    for filename, status, tags in seeded:
        print(f"  {filename:32s} {status:32s} {tags}")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print(
            f"FAILED: {exc.request.method} {exc.request.url} -> {exc.response.status_code} "
            f"{exc.response.text}",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
