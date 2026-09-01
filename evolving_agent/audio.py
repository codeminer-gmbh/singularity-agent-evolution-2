"""Bounded transcription of audio or video evidence through the configured API."""

import json
import os
from pathlib import Path
from typing import Any

from openai import OpenAI, OpenAIError

_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_MAX_OUTPUT_CHARACTERS = 24_000
_SUPPORTED_SUFFIXES = frozenset({
    ".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm",
})


class AudioError(Exception):
    """An audio attachment cannot be transcribed safely or was refused."""


def inspect_audio(path: Path, *, model: str | None = None) -> str:
    """Transcribe one supported local media file without modifying it.

    The transcription endpoint limits uploads to 25 MB.  The returned evidence
    is deliberately capped too, so an unusually long recording cannot consume
    an entire model context.
    """
    suffix = path.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        formats = ", ".join(sorted(_SUPPORTED_SUFFIXES))
        raise AudioError(f"Unsupported audio/video format {suffix or '(none)'}. Supported: {formats}.")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise AudioError(f"Cannot read {path.name}: {error}") from error
    if size > _MAX_UPLOAD_BYTES:
        raise AudioError(f"{path.name} is {size} bytes; transcription accepts files up to {_MAX_UPLOAD_BYTES} bytes.")
    if size == 0:
        raise AudioError(f"{path.name} is empty.")

    key = os.environ.get("OPENAI_API_KEY") or "none-was-configured"
    base_url = os.environ.get("OPENAI_BASE_URL") or None
    chosen_model = model or os.environ.get("OPENAI_TRANSCRIPTION_MODEL") or "gpt-4o-mini-transcribe"
    try:
        client = OpenAI(api_key=key, base_url=base_url, timeout=60.0, max_retries=0)
        with path.open("rb") as media:
            result = client.audio.transcriptions.create(
                model=chosen_model, file=media, response_format="verbose_json"
            )
    except (OSError, OpenAIError) as error:
        raise AudioError(f"Transcription failed: {error}") from error

    if isinstance(result, str):
        payload: dict[str, Any] = {"text": result}
    elif hasattr(result, "model_dump"):
        payload = result.model_dump(exclude_none=True)
    else:
        payload = {"text": str(result)}
    # Keep a useful, valid JSON answer even when a service returns many segments.
    payload["model"] = chosen_model
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(rendered) <= _MAX_OUTPUT_CHARACTERS:
        return rendered
    text = str(payload.get("text", ""))
    overhead = 250
    limited = text[: max(0, _MAX_OUTPUT_CHARACTERS - overhead)]
    return json.dumps(
        {
            "text": limited,
            "truncated": True,
            "notice": f"Transcription output was capped at {_MAX_OUTPUT_CHARACTERS} characters.",
            "model": chosen_model,
        },
        ensure_ascii=False,
        indent=2,
    )
