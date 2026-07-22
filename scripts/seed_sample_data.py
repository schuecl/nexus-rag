"""NFR-9: pre-seed the dev stack with sample documents spanning a range of
Classification/Releasability/Access-scope/Status values, using the seeded
Keycloak realm's test users (infra/keycloak/realm-export), so a fresh
clone-and-run has real data to exercise RBAC scenarios against (allowed
query, denied query, pending vs. approved, curator approve/reject) without
manual setup.

Runs once, after ingestion-api and Keycloak are healthy -- see the
seed-sample-data service in docker-compose.yml. Not idempotent: re-running
against an already-seeded stack creates a second, unrelated copy of each
document rather than detecting and skipping existing ones. That's an
acceptable simplification for a dev convenience script triggered once per
`docker compose up`, not a migration tool.
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://keycloak:8080")
INGESTION_API_URL = os.environ.get("INGESTION_API_URL", "http://ingestion-api:8001")
REALM = "nexus-rag"
CLIENT_ID = "rag-app"
CLIENT_SECRET = os.environ.get("RAG_APP_KEYCLOAK_CLIENT_SECRET", "dev-rag-app-secret")
SEED_PASSWORD = "devpass123"  # matches infra/keycloak/realm-export -- dev-only

READY_TIMEOUT_SECONDS = 120


def wait_until_ready() -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{INGESTION_API_URL}/health", timeout=5).raise_for_status()
            httpx.get(
                f"{KEYCLOAK_URL}/realms/{REALM}/.well-known/openid-configuration", timeout=5
            ).raise_for_status()
            return
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(
        f"ingestion-api/Keycloak not ready after {READY_TIMEOUT_SECONDS}s: {last_error}"
    )


def get_token(username: str) -> str:
    resp = httpx.post(
        f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username": username,
            "password": SEED_PASSWORD,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


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
    return resp.json()


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


def main() -> None:
    print("Waiting for ingestion-api and Keycloak...")
    wait_until_ready()

    alice = get_token("alice-ingest")  # rag-ingest, CUI
    carol = get_token("carol-curator")  # rag-query + rag-curate:USAREUR-AF, SECRET
    dave = get_token("dave-admin")  # all roles, TOP SECRET

    seeded: list[tuple[str, str, str]] = []

    d = submit(
        alice,
        "public-notice.md",
        "# All-Hands Notice\n\nThe cafeteria will be closed for renovations "
        "starting next month. Alternate dining options will be posted on "
        "the intranet.",
        classification="UNCLASSIFIED",
        releasability=["REL TO USA/FVEY"],
        access_scope=["PUBLIC"],
        doc_type="Notice",
    )
    approve(carol, d["id"])
    seeded.append(("public-notice.md", "approved", "UNCLASSIFIED / PUBLIC"))

    d = submit(
        alice,
        "password-policy.md",
        "# Password Policy\n\nAll passwords must be rotated every 90 days and "
        "contain a mix of uppercase, lowercase, and numeric characters. "
        "Reused passwords from the last 12 rotations are rejected.",
        classification="CUI",
        releasability=["REL TO USA/FVEY"],
        access_scope=["USAREUR-AF"],
    )
    approve(carol, d["id"])
    seeded.append(("password-policy.md", "approved", "CUI / USAREUR-AF"))

    d = submit(
        alice,
        "draft-travel-policy.md",
        "# Draft Travel Policy\n\nThis document is still under review and "
        "covers TDY reimbursement procedures for temporary duty travel.",
        classification="CUI",
        releasability=["REL TO USA/FVEY"],
        access_scope=["USAREUR-AF"],
    )
    seeded.append(("draft-travel-policy.md", "pending_review (left unreviewed)", "CUI / USAREUR-AF"))

    d = submit(
        alice,
        "outdated-vpn-guide.md",
        "# VPN Setup Guide (Draft)\n\nThis guide references a VPN client "
        "that has since been deprecated and should not be used.",
        classification="CUI",
        releasability=["REL TO USA/FVEY"],
        access_scope=["USAREUR-AF"],
    )
    reject(carol, d["id"], "References a deprecated VPN client; needs rewrite before publication.")
    seeded.append(("outdated-vpn-guide.md", "rejected", "CUI / USAREUR-AF"))

    d = submit(
        dave,
        "incident-response-plan.md",
        "# Incident Response Plan\n\nUpon detection of a network intrusion, "
        "the Signal Corps duty officer must be notified within 15 minutes "
        "and the affected segment isolated.",
        classification="SECRET",
        releasability=["REL TO USA/FVEY"],
        access_scope=["Signal-Corps"],
    )
    approve(carol, d["id"])
    seeded.append(("incident-response-plan.md", "approved", "SECRET / Signal-Corps"))

    d_v1 = submit(
        alice,
        "network-access-sop-v1.md",
        "# Network Access SOP (v1)\n\nVPN access requires a hardware token "
        "and manager approval.",
        classification="CUI",
        releasability=["REL TO USA/FVEY"],
        access_scope=["USAREUR-AF"],
    )
    approve(carol, d_v1["id"])
    d_v2 = submit(
        alice,
        "network-access-sop-v2.md",
        "# Network Access SOP (v2)\n\nVPN access requires a hardware token, "
        "manager approval, and completion of the annual security awareness "
        "course.",
        classification="CUI",
        releasability=["REL TO USA/FVEY"],
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
        print(f"FAILED: {exc.request.method} {exc.request.url} -> {exc.response.status_code} "
              f"{exc.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
