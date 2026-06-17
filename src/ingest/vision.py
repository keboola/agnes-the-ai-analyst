"""Vision-based text extraction for Tier-2 inputs (images, scanned pages).

The confidence-gated fallback from the design: when a file can't be read by the
lightweight/Docling text path, hand the image to a multimodal model and use its
transcription. GATED and best-effort — needs both the ``anthropic`` SDK and an
API key; without either, ``extract_image_text`` returns ``None`` and the caller
leaves the file ``pending`` (a later run, once configured, can pick it up). Never
raises into the ingest path.

Vision is a *fallback*, not the default: bulk vision-OCR is expensive, so only
Tier-2 files (images today; scanned PDFs once page-rendering lands) take this
route.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_MODEL = os.environ.get("AGNES_VISION_MODEL", "claude-haiku-4-5-20251001")
_EXT_MEDIA = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
_PROMPT = (
    "Transcribe all text in this image verbatim. If it contains tables, render "
    "them as Markdown. Output only the transcription — no commentary."
)


def _api_key() -> Optional[str]:
    return os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY")


def vision_available() -> bool:
    """True when both the anthropic SDK and an API key are present."""
    if _api_key() is None:
        return False
    try:
        import anthropic  # noqa: F401
    except Exception:
        return False
    return True


def media_type_for(ext: str) -> Optional[str]:
    return _EXT_MEDIA.get(ext.lower().lstrip("."))


def extract_image_text(path: str, *, ext: str) -> Optional[str]:
    """OCR/transcribe an image via a multimodal model. None when unavailable.

    Returning None (never raising) keeps the ingest path resilient: no key, no
    SDK, an unsupported media type, or an API error all degrade to "leave it
    pending" rather than failing the file.
    """
    media_type = media_type_for(ext)
    if media_type is None:
        return None
    key = _api_key()
    if key is None:
        return None
    try:
        import anthropic
    except Exception:
        return None
    try:
        with open(path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode("ascii")
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            },
                        },
                        {"type": "text", "text": _PROMPT},
                    ],
                }
            ],
        )
        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        text = "\n".join(parts).strip()
        return text or None
    except Exception as exc:  # pragma: no cover - network/SDK runtime
        logger.warning("vision extract failed for %s: %s", path, exc)
        return None
