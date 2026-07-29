"""Probe the classification-matrix corpus through rag_search, per persona.

The golden-query harness (evaluate_retrieval.py) answers "is the right document
retrievable". This answers the adjacent question the corpus was built for: does
each persona see exactly the documents their claims entitle them to, and no
others.

Attribution is by canary phrase, not by filename. Each corpus document carries a
unique token (MARBLE-HORIZON-7 and friends) repeated through its body, so a hit
is attributable to one source document even when two documents share a filename
at different classifications -- which is precisely the failure mode #226 records
in the filename-matching golden-query harness.

The expectation for each persona is derived here from the manifest and the
realm's role grants, rather than hard-coded, so adding a document to the matrix
extends the test without editing it.

    KEYCLOAK_URL=http://keycloak:8080 ORCHESTRATION_MCP_URL=http://orchestration-mcp:8002 \
      python3 verify_corpus_access.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from _keycloak import get_token

ORCHESTRATION_MCP_URL = os.environ.get("ORCHESTRATION_MCP_URL", "http://orchestration-mcp:8002")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = Path(
    os.environ.get("CORPUS_DIR", REPOSITORY_ROOT / "sample-data" / "classification-corpus")
)
TOP_K = int(os.environ.get("VERIFY_TOP_K", "20"))

# Mirrors infra/keycloak/realm-export. Clearance is a ceiling; releasability is
# a set of holdings, and NONE means "no releasability restriction" so every
# persona can see it regardless of what else they hold.
LADDER = ("UNCLASSIFIED", "CUI", "SECRET")
PERSONAS = {
    "bob-query": {"clearance": "SECRET", "releasability": {"FVEY", "NATO"}},
    "carol-curator": {"clearance": "SECRET", "releasability": {"FVEY", "NATO"}},
    "dave-admin": {"clearance": "SECRET", "releasability": {"FVEY", "NATO", "NOFORN", "USA"}},
}


def may_see(persona: dict, entry: dict) -> bool:
    if LADDER.index(entry["classification"]) > LADDER.index(persona["clearance"]):
        return False
    holdings = set(entry["releasability"])
    if holdings == {"NONE"}:
        return True
    return bool(holdings & persona["releasability"])


def search(client: httpx.Client, token: str, query: str) -> list[dict]:
    response = client.post(
        "/debug/rag_search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "top_k": TOP_K},
    )
    response.raise_for_status()
    return response.json().get("results", [])


def main() -> int:
    manifest = json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
    failures = 0

    with httpx.Client(base_url=ORCHESTRATION_MCP_URL, timeout=180) as client:
        for username, persona in PERSONAS.items():
            token = get_token(username)
            expected = {e["canary"] for e in manifest if may_see(persona, e)}
            forbidden = {e["canary"] for e in manifest} - expected

            # One query per document, using its own topic, so a miss means "not
            # retrievable by this persona" rather than "outranked by another
            # document" -- a single broad query cannot distinguish those.
            seen: set[str] = set()
            for entry in manifest:
                for result in search(client, token, entry["title"]):
                    text = json.dumps(result)
                    seen.update(c for c in {e["canary"] for e in manifest} if c in text)

            missing = sorted(expected - seen)
            leaked = sorted(seen & forbidden)

            status = "OK  " if not leaked and not missing else "FAIL"
            if leaked or missing:
                failures += 1
            print(
                f"  {status} {username:15s} clearance={persona['clearance']:12s} "
                f"rel={'/'.join(sorted(persona['releasability'])):20s} "
                f"visible={len(seen & expected)}/{len(expected)}"
            )
            for canary in leaked:
                entry = next(e for e in manifest if e["canary"] == canary)
                print(
                    f"       LEAK    {canary} -- {entry['filename']} "
                    f"({entry['classification']}, {'/'.join(entry['releasability'])})"
                )
            for canary in missing:
                entry = next(e for e in manifest if e["canary"] == canary)
                print(
                    f"       MISSING {canary} -- {entry['filename']} "
                    f"({entry['classification']}, {'/'.join(entry['releasability'])})"
                )

    print()
    if failures:
        print(f"{failures} persona(s) failed: a LEAK is an FR-26 access-control defect;")
        print("a MISSING is either an ingestion gap or a retrieval-recall problem.")
        return 1
    print("All personas saw exactly the documents their claims entitle them to.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
