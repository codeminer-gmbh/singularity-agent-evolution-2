"""Bounded metadata, subtitle, and frame-text inspection for media evidence."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Callable

MAX_TEXT_CHARACTERS = 24_000
MAX_SUBTITLE_BYTES = 1_000_000
_SUPPORTED_SUFFIXES = {
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".wma",
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".mpeg", ".mpg", ".m4v",
}


class MediaError(Exception):
    """A media attachment could not be safely inspected."""


def inspect_media(
    path: Path,
    *,
    timestamp: float | None = None,
    run: Callable[[list[str]], tuple[int | None, str, str]],
) -> str:
    """Return stream metadata, embedded captions, and optional OCR of one frame.

    The function never changes the supplied attachment.  FFmpeg writes only to
    a private temporary directory, and output is bounded before it reaches the
    model.  ``timestamp`` is useful for a video slide or a visual cue; omitted
    timestamps avoid decoding a frame altogether.
    """
    if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise MediaError("Supported media formats include common audio and video files (MP3/WAV/M4A/OGG/FLAC/MP4/MKV/MOV/AVI/WebM).")
    if timestamp is not None and (timestamp < 0 or timestamp > 86_400):
        raise MediaError("'timestamp' must be between 0 and 86400 seconds.")
    code, stdout, stderr = run([
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format",
        "-show_streams", str(path),
    ])
    if code != 0:
        raise MediaError(f"ffprobe could not read the media: {_reason(stderr)}")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise MediaError("ffprobe returned malformed metadata.") from error
    streams = data.get("streams")
    if not isinstance(streams, list) or not streams:
        raise MediaError("The media contains no readable streams.")

    lines = [_summary(path.name, data), "Streams:"]
    subtitle_indexes: list[int] = []
    for index, stream in enumerate(streams):
        if not isinstance(stream, dict):
            continue
        kind = str(stream.get("codec_type", "unknown"))
        codec = str(stream.get("codec_name", "unknown"))
        language = str((stream.get("tags") or {}).get("language", "und"))
        detail = f"  {index}: {kind} ({codec}, language={language})"
        if kind == "video":
            detail += _video_detail(stream)
        elif kind == "audio":
            detail += _audio_detail(stream)
        lines.append(detail)
        if kind == "subtitle":
            subtitle_indexes.append(index)
    if subtitle_indexes:
        lines.append("Embedded subtitles (first readable stream):\n---\n" + _subtitles(path, run))
    if timestamp is not None:
        lines.append(f"Frame OCR at {timestamp:g}s:\n---\n" + _frame_ocr(path, timestamp, run))
    return _bounded("\n".join(lines))


def _summary(name: str, data: dict[object, object]) -> str:
    fmt = data.get("format")
    fmt = fmt if isinstance(fmt, dict) else {}
    duration = fmt.get("duration", "unknown")
    size = fmt.get("size", "unknown")
    names = fmt.get("format_name", "unknown")
    return f"Media: {name} (format={names}, duration={duration}s, size={size} bytes)"


def _video_detail(stream: dict[object, object]) -> str:
    width, height = stream.get("width"), stream.get("height")
    rate = stream.get("avg_frame_rate", "?")
    return f", {width}x{height}, {rate} fps"


def _audio_detail(stream: dict[object, object]) -> str:
    return f", {stream.get('sample_rate', '?')} Hz, {stream.get('channels', '?')} channels"


def _subtitles(path: Path, run: Callable[[list[str]], tuple[int | None, str, str]]) -> str:
    with tempfile.TemporaryDirectory(prefix="agent-media-") as directory:
        destination = Path(directory) / "captions.srt"
        code, _, stderr = run([
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:s:0",
            "-c:s", "srt", "-y", str(destination),
        ])
        if code != 0 or not destination.is_file():
            return f"Captions could not be extracted: {_reason(stderr)}"
        content = destination.read_bytes()[: MAX_SUBTITLE_BYTES + 1]
    if len(content) > MAX_SUBTITLE_BYTES:
        return f"[caption extraction truncated at {MAX_SUBTITLE_BYTES} bytes]\n" + content[:MAX_SUBTITLE_BYTES].decode("utf-8", "replace")
    text = content.decode("utf-8", "replace").strip()
    return text or "[The subtitle stream contains no text.]"


def _frame_ocr(path: Path, timestamp: float, run: Callable[[list[str]], tuple[int | None, str, str]]) -> str:
    with tempfile.TemporaryDirectory(prefix="agent-media-") as directory:
        image = Path(directory) / "frame.png"
        code, _, stderr = run([
            "ffmpeg", "-v", "error", "-nostdin", "-ss", str(timestamp), "-i", str(path),
            "-frames:v", "1", "-y", str(image),
        ])
        if code != 0 or not image.is_file():
            return f"Frame could not be extracted: {_reason(stderr)}"
        code, text, stderr = run(["tesseract", str(image), "stdout", "--psm", "3"])
    if code != 0:
        return f"Frame OCR failed: {_reason(stderr)}"
    return text.strip() or "[No text recognized in this frame.]"


def _bounded(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARACTERS:
        return text
    return text[:MAX_TEXT_CHARACTERS] + f"\n... [truncated at {MAX_TEXT_CHARACTERS} characters]"


def _reason(error: str) -> str:
    return error.strip()[:500] or "no diagnostic was supplied"
