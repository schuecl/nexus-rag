"""Upload and approve the supported files in sample-data/upload-tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
from _keycloak import get_token

INGESTION_API_URL = os.environ.get("INGESTION_API_URL", "http://ingestion-api:8001")
PROCESSING_TIMEOUT_SECONDS = int(os.environ.get("PROCESSING_TIMEOUT_SECONDS", "180"))
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = REPOSITORY_ROOT / "sample-data" / "upload-tests"
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".html"}
SOURCE_ORIGINATOR = "Nexus RAG upload test corpus"


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def wait_for_processing(client: httpx.Client, token: str, doc_id: str) -> dict:
    deadline = time.monotonic() + PROCESSING_TIMEOUT_SECONDS
    last_status = "queued"
    while time.monotonic() < deadline:
        response = client.get(f"/documents/{doc_id}", headers=headers(token))
        response.raise_for_status()
        document = response.json()
        last_status = document["status"]
        if last_status not in {"queued", "processing"}:
            return document
        time.sleep(1)
    raise RuntimeError(
        f"document {doc_id} did not finish within {PROCESSING_TIMEOUT_SECONDS}s "
        f"(last status: {last_status})"
    )


def main() -> None:
    files = sorted(
        path
        for path in CORPUS_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not files:
        raise RuntimeError(f"no supported test documents found in {CORPUS_DIR}")

    token = get_token("dave-admin")
    imported = 0
    skipped = 0
    with httpx.Client(base_url=INGESTION_API_URL, timeout=60) as client:
        response = client.get("/documents/mine", headers=headers(token))
        response.raise_for_status()
        approved = {
            document["filename"]
            for document in response.json()
            if document["status"] == "approved"
            and document["source_originator"] == SOURCE_ORIGINATOR
        }

        for path in files:
            if path.name in approved:
                skipped += 1
                print(f"SKIP     {path.name} (already approved)")
                continue
            print(f"UPLOAD   {path.name}")
            response = client.post(
                "/documents",
                headers=headers(token),
                files={"file": (path.name, path.read_bytes())},
                data={
                    "classification": "UNCLASSIFIED",
                    "releasability": json.dumps(["NONE"]),
                    "access_scope": json.dumps(["ALL_AUTHENTICATED"]),
                    "source_originator": SOURCE_ORIGINATOR,
                    "doc_type": "Upload test document",
                    "program_community": "Nexus RAG",
                },
            )
            response.raise_for_status()
            processed = wait_for_processing(client, token, response.json()["id"])
            if processed["status"] != "pending_review":
                raise RuntimeError(
                    f"{path.name} reached {processed['status']}: "
                    f"{processed.get('processing_error') or 'unknown processing error'}"
                )
            response = client.post(
                f"/curate/{processed['id']}/approve",
                headers=headers(token),
            )
            response.raise_for_status()
            imported += 1
            print(f"APPROVED {path.name} ({processed['id']})")

    print(
        f"\nUpload test corpus ready for RAG: {imported} imported, "
        f"{skipped} already present, {len(files)} total supported files."
    )
    print("PNG and JPG samples exercise the OCR no-readable-text failure path (#241).")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print(
            f"FAILED: {exc.request.method} {exc.request.url} -> "
            f"{exc.response.status_code} {exc.response.text}",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
