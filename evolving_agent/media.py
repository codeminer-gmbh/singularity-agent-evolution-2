"""Bounded speech-to-text evidence extraction for local media attachments.

The transcription endpoint accepts the audio track in common audio/video
containers.  It is deliberately read-only: the source file is uploaded but is
not converted, copied, or changed in the task workspace.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from openai import OpenAI, OpenAIError

_MAX_MEDIA_BYTES = 24 * 1024 * 1024
_MAX_TRANSCRIPT_CHARACTERS = 24_000
_SUPPORTED_SUFFIXES = frozenset({
    ".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm",
})


class MediaError(Exception):
    """A media attachment cannot safely be transcribed."""


def transcribe_media(path: Path, model: str, timeout_seconds: int) -> str:
    """Return bounded spoken evidence and timestamps from one media file.

    The OpenAI key and optional compatible base URL remain in the process
    environment, just as they do for the agent's reasoning client.  A smaller
    than API-limit upload cap leaves room for multipart encoding and prevents a
    task attachment from consuming an unbounded request.
    """
    if not path.is_file():
        raise MediaError("The media path is not a regular file.")
    if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(_SUPPORTED_SUFFIXES))
        raise MediaError(f"Unsupported media type; supported suffixes: {supported}.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise MediaError(f"Could not stat media file: {exc}") from exc
    if size > _MAX_MEDIA_BYTES:
        raise MediaError(
            f"Media file is {size} bytes; transcription accepts at most {_MAX_MEDIA_BYTES} bytes."
        )
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise MediaError("OPENAI_API_KEY is required to transcribe media.")

    try:
        client = OpenAI(timeout=timeout_seconds)
        with path.open("rb") as audio:
            result = client.audio.transcriptions.create(
                model=model,
                file=audio,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
    except (OSError, OpenAIError) as exc:
        raise MediaError(f"Transcription request failed: {exc}") from exc

    text = _field(result, "text")
    if not isinstance(text, str) or not text.strip():
        raise MediaError("The transcription service returned no readable text.")
    lines = ["Transcript:", text.strip()]
    segments = _field(result, "segments")
    if isinstance(segments, list) and segments:
        lines.append("\nTimestamped segments:")
        for segment in segments:
            start = _field(segment, "start")
            end = _field(segment, "end")
            spoken = _field(segment, "text")
            if isinstance(spoken, str):
                lines.append(f"[{_timestamp(start)}–{_timestamp(end)}] {spoken.strip()}")
    rendered = "\n".join(lines)
    if len(rendered) > _MAX_TRANSCRIPT_CHARACTERS:
        return rendered[:_MAX_TRANSCRIPT_CHARACTERS] + "\n... [transcript truncated]"
    return rendered


def _field(value: Any, name: str) -> Any:
    """Read a field from either an SDK model or a test-friendly mapping."""
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _timestamp(value: Any) -> str:
    """Render API segment times compactly without trusting their exact type."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minutes, seconds = divmod(float(value), 60)
        return f"{int(minutes):02d}:{seconds:05.2f}"
    return "?:??"
