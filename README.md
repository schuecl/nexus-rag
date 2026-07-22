# nexus-rag
Enterprise grade RAG infrastructure

See `REQUIREMENTS.md` for the full spec (MPNexus RAG pipeline). Two ways to run it:

- **Local dev**: `docker compose up` — see `docs/dev-setup.md` for a one-command
  stand-up of the full ingest → curate → query flow, pre-seeded with sample data.
- **Production (Kubernetes)**: `helm/nexus-rag/` — see `helm/nexus-rag/README.md`.
  Scoped to only the new components this project adds (NFR-10); assumes
  LibreChat, LiteLLM, Keycloak, and the cluster's existing vLLM/Ollama are
  already deployed.
