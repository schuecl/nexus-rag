# Upload test corpus

These harmless files exercise every document format supported by the local
ingestion worker. Their content is fictional and intentionally includes
memorable facts for retrieval testing.

Supported files are uploaded and approved by:

```bash
KEYCLOAK_URL=http://localhost:8080 \
INGESTION_API_URL=http://localhost:8001 \
python scripts/ingest_upload_test_files.py
```

The PNG and JPG files were negative-test fixtures until issue #241: image
uploads are now supported document formats, OCR'd by the worker
(`services/ingestion-worker/app/ocr.py`). These two are photographs without
legible text, so a submission is accepted, OCR finds nothing to read, and the
document reaches `failed` with an actionable "no readable text" message --
they now exercise the no-text failure path rather than the extension gate.
