"""Model catalog helpers shared by AI Studio discovery and API routes."""

from __future__ import annotations

import re
from collections.abc import Iterable

FALLBACK_GEMINI_MODELS = (
    "gemini-3-flash-preview",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-image-preview",
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-live-preview",
    "gemini-3.1-flash-tts-preview",
    "gemini-pro-latest",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
)

_NON_MODEL_GEMINI_LABELS = {"gemini-api", "gemini-app", "gemini-model"}


def normalize_gemini_model(value: object) -> str | None:
    """Normalize an AI Studio label/id and reject non-Gemini products."""
    text = str(value or "").strip().lower()
    match = re.search(r"\bgemini(?:[-_ ]+[a-z0-9][a-z0-9._-]*)+", text, re.IGNORECASE)
    if not match:
        return None
    model_id = re.sub(r"[^a-z0-9._-]+", "-", match.group(0).lower().replace("_", "-"))
    model_id = re.sub(r"-+", "-", model_id).strip("-")
    if model_id in _NON_MODEL_GEMINI_LABELS:
        return None
    return model_id if model_id.startswith("gemini-") else None


def filter_gemini_models(values: Iterable[object]) -> list[str]:
    """Return stable, deduplicated Gemini-only model ids."""
    return sorted({model for value in values if (model := normalize_gemini_model(value))})


def model_metadata(model_id: str) -> dict[str, object]:
    """Return conservative metadata for model-aware clients such as dsh."""
    normalized = model_id.removeprefix("models/").lower()
    is_special_output = any(token in normalized for token in ("image", "live", "tts"))
    return {
        "contextWindow": 1_000_000,
        "maxTokens": 65_536,
        "reasoning": not is_special_output,
        "inputModalities": ["text", "image"],
    }
