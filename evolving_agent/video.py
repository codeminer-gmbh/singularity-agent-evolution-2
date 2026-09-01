"""Bounded OCR of sampled frames from local video evidence."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable

_MAX_INPUT_BYTES = 500 * 1024 * 1024
_MAX_DURATION_SECONDS = 4 * 60 * 60
_MAX_FRAMES = 12
_MAX_OUTPUT_CHARACTERS = 24_000
_SUPPORTED_SUFFIXES = frozenset({
    ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm",
})


class VideoError(Exception):
    """Video evidence cannot be sampled or OCRed."""


def inspect_video(
    path: Path,
    *,
    max_frames: int = 6,
    run: Callable[[list[str]], tuple[int | None, str, str]],
) -> str:
    """OCR evenly-spaced video frames without writing beside the input.

    Sampling makes on-screen slides and captions accessible without decoding a
    whole recording.  Frames live only in a private temporary directory and
    every decoder/OCR invocation uses the caller's bounded command runner.
    """
    if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
        formats = ", ".join(sorted(_SUPPORTED_SUFFIXES))
        raise VideoError(f"Unsupported video format {path.suffix or '(none)'}. Supported: {formats}.")
    if not 1 <= max_frames <= _MAX_FRAMES:
        raise VideoError(f"max_frames must be from 1 through {_MAX_FRAMES}.")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise VideoError(f"Cannot read {path.name}: {error}") from error
    if size == 0:
        raise VideoError(f"{path.name} is empty.")
    if size > _MAX_INPUT_BYTES:
        raise VideoError(f"{path.name} is {size} bytes; video inspection accepts files up to {_MAX_INPUT_BYTES} bytes.")

    code, stream, error = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=codec_type", "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    if code != 0 or stream.strip() != "video":
        raise VideoError(f"ffprobe could not find a video stream: {_reason(error)}")
    code, duration_text, error = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
        "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    if code != 0:
        raise VideoError(f"ffprobe could not determine video duration: {_reason(error)}")
    try:
        duration = float(duration_text.strip())
    except ValueError as invalid:
        raise VideoError("ffprobe returned no usable video duration.") from invalid
    if duration <= 0 or duration != duration:
        raise VideoError("Video duration must be positive.")
    if duration > _MAX_DURATION_SECONDS:
        raise VideoError(f"Video is {duration:.1f} seconds long; inspection limit is {_MAX_DURATION_SECONDS} seconds.")

    timestamps = _timestamps(duration, max_frames)
    sections: list[str] = []
    with tempfile.TemporaryDirectory(prefix="agent-video-ocr-") as directory:
        for index, timestamp in enumerate(timestamps, start=1):
            image = Path(directory) / f"frame-{index}.png"
            code, _, error = run([
                "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{timestamp:.3f}",
                "-i", str(path), "-frames:v", "1", "-an", "-y", str(image),
            ])
            if code != 0 or not image.is_file():
                raise VideoError(f"Could not decode frame at {timestamp:.1f}s: {_reason(error)}")
            code, text, error = run(["tesseract", str(image), "stdout", "--psm", "6"])
            if code != 0:
                raise VideoError(f"Tesseract could not OCR frame at {timestamp:.1f}s: {_reason(error)}")
            cleaned = text.strip() or "[No text recognized in this frame.]"
            sections.append(f"Frame {index}/{len(timestamps)} at {timestamp:.1f}s\n---\n{cleaned}")

    rendered = f"Video frame OCR ({path.name}; duration {duration:.1f}s)\n===\n" + "\n\n".join(sections)
    if len(rendered) <= _MAX_OUTPUT_CHARACTERS:
        return rendered
    return f"{rendered[:_MAX_OUTPUT_CHARACTERS]}\n... [truncated at {_MAX_OUTPUT_CHARACTERS} characters]"


def _timestamps(duration: float, count: int) -> tuple[float, ...]:
    """Return distinct representative positions, avoiding an exact end seek."""
    last = max(0.0, duration - 0.05)
    if count == 1:
        return (min(duration / 2, last),)
    return tuple(last * index / (count - 1) for index in range(count))


def _reason(error: str) -> str:
    return error.strip() or "no diagnostic was returned"
