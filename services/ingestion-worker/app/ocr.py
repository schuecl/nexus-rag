"""Issue #241: OCR for content whose text lives in pixels -- standalone image
uploads and scanned (image-only) PDF pages. Without this, a scanned document
fails ingestion with "no extractable text" and an image upload is rejected
outright, so the content is simply absent from the corpus.

Deliberately not #92's captioning (app/captioning.py, PR #240): captioning
*describes* a figure via an opt-in vision model over the network; OCR extracts
a page's literal text -- deterministic, local, no model service in the loop.
The two are complementary treatments for different content.

Engine: Tesseract via pytesseract, baked into the worker image (Dockerfile) --
no runtime downloads (NFR-1 air-gap) and no network call at OCR time. OCR runs
inside the parse stage, so #208's PROCESSING_TIMEOUT_SECONDS already bounds
it; MAX_OCR_IMAGES_PER_DOCUMENT and the minimum-dimension filter bound the
per-document work, and Pillow's own decompression-bomb guard covers the
decode.

Failure semantics are split by honesty, not uniformity (see parsing.py):
`ocr_image` itself degrades to "" on any per-image failure, because for a
mixed PDF a broken OCR must cost only what today already contributes nothing.
Whether "" is then a skip (scanned-page fallback) or a ParsingError (an image
upload, where OCR is the only content path) is the caller's decision.
"""

from __future__ import annotations

import io
import logging
import os
from functools import cache

from common.log_safety import log_safe

logger = logging.getLogger("ingestion-worker")

# Tesseract language(s), e.g. "eng" or "eng+deu". The worker image bakes in
# eng; other languages need their traineddata added to the image (air-gap:
# never downloaded at runtime).
OCR_LANG = os.environ.get("OCR_LANG", "eng")

# Below this an image cannot hold legible text -- it's a bullet glyph or a
# border, and Tesseract on noise produces noise, which would then be embedded.
OCR_MIN_IMAGE_DIMENSION = int(os.environ.get("OCR_MIN_IMAGE_DIMENSION", "32"))

# Bound on OCR'd images per document, shared across the scanned-page fallback
# (parsing.py). A 500-page scan is legitimate but must not monopolize the
# worker; the pages past the cap are logged, not silently dropped.
MAX_OCR_IMAGES_PER_DOCUMENT = int(os.environ.get("MAX_OCR_IMAGES_PER_DOCUMENT", "50"))


@cache
def ocr_available() -> bool:
    """Whether the tesseract binary is actually invocable -- checked once per
    process (the answer cannot change without a container rebuild)."""
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
    except Exception as exc:
        logger.warning(
            "tesseract is not available, OCR disabled: %s: %s",
            type(exc).__name__,
            log_safe(exc),
        )
        return False
    return True


def ocr_image(data: bytes) -> str:
    """Text recognized in one image, or "" -- for an undecodable image, one
    too small to hold legible text, or any tesseract failure. Never raises:
    the caller decides whether an empty result is a skip or an error."""
    try:
        import pytesseract
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width < OCR_MIN_IMAGE_DIMENSION or height < OCR_MIN_IMAGE_DIMENSION:
                return ""
            text = pytesseract.image_to_string(image, lang=OCR_LANG)
    except Exception as exc:
        logger.warning("OCR failed on an image: %s: %s", type(exc).__name__, log_safe(exc))
        return ""
    return text.strip()
