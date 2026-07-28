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

The PNG and JPG files are negative-test fixtures. Image-only uploads are not
supported document formats, so the responsive browser form should reject them
before submission. A direct API submission is accepted into the asynchronous
queue and then reaches `failed` when the worker validates the file extension.
