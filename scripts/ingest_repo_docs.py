"""Import this repository's Markdown documentation into the dev RAG.

The importer deliberately uses the same authenticated upload, background
processing, and curator approval APIs as the browser.  Documents are tagged
UNCLASSIFIED / NONE / ALL_AUTHENTICATED so every authenticated dev user can
query the project documentation.

The script is safe to rerun: an already-approved repository document with the
same relative filename is skipped.  Failed or rejected prior attempts do not
block a fresh submission.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed argv, no shell; see repository_markdown_files
import sys
import time
from pathlib import Path

import httpx
from _keycloak import get_token

INGESTION_API_URL = os.environ.get("INGESTION_API_URL", "http://ingestion-api:8001")
PROCESSING_TIMEOUT_SECONDS = int(os.environ.get("PROCESSING_TIMEOUT_SECONDS", "180"))
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ORIGINATOR = "nexus-rag repository"
DOCUMENT_TYPE = "Project documentation"


def _git_executable() -> str:
    """Absolute path to git, resolved once.

    bandit's B607 flags a partial executable path, and it is right to: a bare
    "git" resolves through PATH, so whatever PATH happens to contain when this
    runs decides what executes. Resolving it here means the lookup happens
    once, visibly, and a missing git is an immediate clear error rather than a
    confusing failure inside subprocess.
    """
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH; this script reads the repo's file list with it")
    return git


def repository_markdown_files() -> list[Path]:
    """Return tracked and untracked, non-ignored repository Markdown files."""
    # No shell, a fixed argument list, and no interpolation of anything
    # caller-supplied -- the only variable is REPOSITORY_ROOT, which is derived
    # from __file__. B404's concern (arbitrary command execution) does not
    # apply to this shape.
    result = subprocess.run(  # nosec B603 - fixed argv, no shell, no caller input
        [_git_executable(), "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.md"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        REPOSITORY_ROOT / relative_path
        for relative_path in result.stdout.splitlines()
        if relative_path.strip() and not relative_path.startswith("sample-data/upload-tests/")
    ]


def request_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def existing_documents(client: httpx.Client, token: str) -> list[dict]:
    response = client.get("/documents/mine", headers=request_headers(token))
    response.raise_for_status()
    return response.json()


def submit_document(client: httpx.Client, token: str, path: Path) -> dict:
    relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
    response = client.post(
        "/documents",
        headers=request_headers(token),
        files={"file": (relative_path, path.read_bytes(), "text/markdown")},
        data={
            "classification": "UNCLASSIFIED",
            "releasability": json.dumps(["NONE"]),
            "access_scope": json.dumps(["ALL_AUTHENTICATED"]),
            "source_originator": SOURCE_ORIGINATOR,
            "doc_type": DOCUMENT_TYPE,
            "program_community": "Nexus RAG",
        },
    )
    response.raise_for_status()
    return response.json()


def wait_for_processing(client: httpx.Client, token: str, doc_id: str) -> dict:
    deadline = time.monotonic() + PROCESSING_TIMEOUT_SECONDS
    last_status = "queued"
    while time.monotonic() < deadline:
        response = client.get(
            f"/documents/{doc_id}",
            headers=request_headers(token),
        )
        response.raise_for_status()
        document = response.json()
        last_status = document["status"]
        if last_status not in {"queued", "processing"}:
            return document
        time.sleep(1)
    raise RuntimeError(
        f"document {doc_id} did not finish processing within "
        f"{PROCESSING_TIMEOUT_SECONDS}s (last status: {last_status})"
    )


def approve_document(client: httpx.Client, token: str, doc_id: str) -> dict:
    response = client.post(
        f"/curate/{doc_id}/approve",
        headers=request_headers(token),
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    files = repository_markdown_files()
    if not files:
        raise RuntimeError("no tracked Markdown documentation was found")

    token = get_token("dave-admin")
    imported: list[str] = []
    skipped: list[str] = []

    with httpx.Client(base_url=INGESTION_API_URL, timeout=60) as client:
        current = existing_documents(client, token)
        approved_filenames = {
            document["filename"]
            for document in current
            if document["status"] == "approved"
            and document["source_originator"] == SOURCE_ORIGINATOR
        }

        for path in files:
            relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
            if relative_path in approved_filenames:
                skipped.append(relative_path)
                print(f"SKIP     {relative_path} (already approved)")
                continue

            print(f"UPLOAD   {relative_path}")
            submitted = submit_document(client, token, path)
            processed = wait_for_processing(client, token, submitted["id"])
            if processed["status"] != "pending_review":
                detail = processed.get("processing_error") or "no processing error was provided"
                raise RuntimeError(
                    f"{relative_path} reached {processed['status']} instead of "
                    f"pending_review: {detail}"
                )
            approved = approve_document(client, token, submitted["id"])
            if approved["status"] != "approved":
                raise RuntimeError(
                    f"{relative_path} approval returned unexpected status {approved['status']}"
                )
            imported.append(relative_path)
            print(f"APPROVED {relative_path} ({approved['id']})")

    print(
        f"\nRepository documentation ready for RAG: "
        f"{len(imported)} imported, {len(skipped)} already present, "
        f"{len(files)} total."
    )


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
