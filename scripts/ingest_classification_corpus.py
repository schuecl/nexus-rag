"""Upload, process and approve the classification-matrix corpus.

Companion to create_classification_corpus.py. Uploads every document with the
classification and releasability the manifest assigns it, waits for the worker
to finish, and approves it -- leaving a corpus that spans the whole ladder and
is therefore usable for testing retrieval *access control* rather than only
retrieval quality.

Uploads as dave-admin, who is the only seeded user holding both SECRET
clearance and all four releasability values (see infra/keycloak/realm-export).
alice-ingest holds CUI/FVEY only and would be refused on most of the matrix --
correctly, which verify_corpus_placement.py exercises deliberately.

Run against a stack that is already up:

    KEYCLOAK_URL=http://127.0.0.1:8080 INGESTION_API_URL=http://127.0.0.1:8001 \
      python scripts/ingest_classification_corpus.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
from _keycloak import get_token

INGESTION_API_URL = os.environ.get("INGESTION_API_URL", "http://ingestion-api:8001")
PROCESSING_TIMEOUT_SECONDS = int(os.environ.get("PROCESSING_TIMEOUT_SECONDS", "300"))
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# Overridable because scripts/Dockerfile flattens the scripts into /srv, so the
# repository-relative default does not survive into the container image -- the
# compose service bind-mounts the corpus and points this at it.
CORPUS_DIR = Path(
    os.environ.get("CORPUS_DIR", REPOSITORY_ROOT / "sample-data" / "classification-corpus")
)
SOURCE_ORIGINATOR = "Nexus RAG classification matrix corpus"
UPLOADER = os.environ.get("CORPUS_UPLOADER", "dave-admin")


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def wait_for_processing(client: httpx.Client, token: str, doc_id: str) -> dict:
    deadline = time.monotonic() + PROCESSING_TIMEOUT_SECONDS
    last = "queued"
    while time.monotonic() < deadline:
        response = client.get(f"/documents/{doc_id}", headers=headers(token))
        response.raise_for_status()
        document = response.json()
        last = document["status"]
        if last not in {"queued", "processing"}:
            return document
        time.sleep(1)
    raise RuntimeError(f"{doc_id} stuck in {last} after {PROCESSING_TIMEOUT_SECONDS}s")


def main() -> int:
    manifest_path = CORPUS_DIR / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(
            f"{manifest_path} missing -- run scripts/create_classification_corpus.py first"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    token = get_token(UPLOADER)
    imported, skipped, failed = 0, 0, 0

    with httpx.Client(base_url=INGESTION_API_URL, timeout=120) as client:
        response = client.get("/documents/mine", headers=headers(token))
        response.raise_for_status()
        mine = [d for d in response.json() if d["source_originator"] == SOURCE_ORIGINATOR]
        # Keyed by (filename, classification): the same filename may legitimately
        # exist at two levels, and treating filename alone as the identity is
        # exactly the mistake #226 records in the golden-query harness.
        existing = {(d["filename"], d["classification"]) for d in mine if d["status"] == "approved"}
        # Embedding on a CPU-only host is slow enough that a previous run can
        # time out waiting while the worker is still making progress. Those
        # documents are already parsed and embedded -- re-uploading them would
        # duplicate the corpus, so they are picked up and approved instead.
        awaiting = {
            (d["filename"], d["classification"]): d["id"]
            for d in mine
            if d["status"] == "pending_review"
        }

        for entry in manifest:
            name = entry["filename"]
            path = CORPUS_DIR / name
            key = (name, entry["classification"])
            if key in existing:
                skipped += 1
                print(f"  SKIP     {name:38s} already approved at {entry['classification']}")
                continue
            if key in awaiting:
                client.post(
                    f"/curate/{awaiting[key]}/approve", headers=headers(token)
                ).raise_for_status()
                imported += 1
                print(f"  APPROVED {name:38s} {entry['classification']:13s} (was pending)")
                continue

            try:
                response = client.post(
                    "/documents",
                    headers=headers(token),
                    files={"file": (name, path.read_bytes())},
                    data={
                        "classification": entry["classification"],
                        "releasability": json.dumps(entry["releasability"]),
                        "access_scope": json.dumps(entry["access_scope"]),
                        "source_originator": SOURCE_ORIGINATOR,
                        "doc_type": "Classification matrix test document",
                        "program_community": "Northwind Support Activity",
                    },
                )
                response.raise_for_status()
                doc = wait_for_processing(client, token, response.json()["id"])
                if doc["status"] != "pending_review":
                    failed += 1
                    print(
                        f"  FAIL     {name:38s} {doc['status']}: "
                        f"{doc.get('processing_error') or 'unknown'}"
                    )
                    continue
                client.post(
                    f"/curate/{doc['id']}/approve", headers=headers(token)
                ).raise_for_status()
                imported += 1
                print(
                    f"  APPROVED {name:38s} {entry['classification']:13s} "
                    f"{'/'.join(entry['releasability']):6s} chunks={doc.get('chunk_count')} "
                    f"{doc['id']}"
                )
            except httpx.HTTPStatusError as exc:
                failed += 1
                print(
                    f"  FAIL     {name:38s} HTTP {exc.response.status_code} "
                    f"{exc.response.text[:160]}"
                )

    print(
        f"\n{imported} approved, {skipped} already present, {failed} failed, {len(manifest)} total"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
