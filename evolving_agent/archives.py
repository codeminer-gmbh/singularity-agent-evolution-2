"""Bounded, non-extracting inspection for ordinary ZIP and TAR archives.

Archive evidence commonly contains a report or dataset one level below its
container.  This module inventories that container and previews a specifically
requested text member without writing archive contents to disk.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import tarfile
from typing import Any
import zipfile


class ArchiveError(Exception):
    """The archive cannot safely be inspected as requested."""


_SUPPORTED_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
_MAX_PREVIEW_BYTES = 1_000_000


def inspect_archive(
    path: Path,
    *,
    member: str | None = None,
    max_entries: int = 100,
    max_characters: int = 8_000,
) -> str:
    """Return JSON archive metadata, an entry inventory, and optional text preview.

    The implementation never calls ``extract``.  A preview reads at most a
    modest bounded prefix from one regular member, so an archive cannot cause
    a materialized file or an unbounded decompression operation.
    """
    _validate_limits(max_entries, max_characters)
    _validate_archive_path(path)
    normalized_member = _validate_member_name(member) if member is not None else None
    try:
        if zipfile.is_zipfile(path):
            result = _inspect_zip(path, normalized_member, max_entries, max_characters)
        elif tarfile.is_tarfile(path):
            result = _inspect_tar(path, normalized_member, max_entries, max_characters)
        else:
            raise ArchiveError("Unsupported or invalid archive; supported formats are ZIP and TAR variants.")
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        raise ArchiveError(f"Could not read archive: {exc}") from exc
    return json.dumps(result, ensure_ascii=False, indent=2)


def _validate_limits(entries: int, characters: int) -> None:
    if not isinstance(entries, int) or isinstance(entries, bool) or not 1 <= entries <= 500:
        raise ArchiveError("max_entries must be an integer from 1 to 500.")
    if not isinstance(characters, int) or isinstance(characters, bool) or not 1 <= characters <= 12_000:
        raise ArchiveError("max_characters must be an integer from 1 to 12000.")


def _validate_archive_path(path: Path) -> None:
    if not path.is_file():
        raise ArchiveError("Archive path does not name a regular file.")
    lowered = path.name.lower()
    if not lowered.endswith(_SUPPORTED_SUFFIXES):
        raise ArchiveError("Unsupported archive extension; use ZIP or TAR (.tar.gz, .tgz, .tar.bz2, .tar.xz).")


def _validate_member_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ArchiveError("member must be a non-empty string.")
    # Archive paths always use POSIX separators. Do not interpret a member as a
    # host filesystem path, and require an exact safe archive name.
    candidate = PurePosixPath(name)
    if candidate.is_absolute() or ".." in candidate.parts or name.startswith("/"):
        raise ArchiveError("member must be a relative archive member name without '..'.")
    return name


def _entry(name: str, size: int, compressed_size: int | None, kind: str, modified: str | None) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "type": kind, "size_bytes": size}
    if compressed_size is not None:
        item["compressed_size_bytes"] = compressed_size
    if modified is not None:
        item["modified"] = modified
    return item


def _preview(stream: io.BufferedIOBase | Any, max_characters: int) -> dict[str, Any]:
    # UTF-8 can take four bytes per character. Extra bytes also make truncated
    # replacement sequences unlikely, while this hard cap remains small.
    byte_limit = min(_MAX_PREVIEW_BYTES, max_characters * 4 + 16)
    raw = stream.read(byte_limit)
    try:
        text = raw.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        encoding = "utf-8 (replacement for invalid bytes)"
    truncated = len(raw) == byte_limit
    return {
        "encoding": encoding,
        "text_preview": text[:max_characters],
        "truncated": truncated or len(text) > max_characters,
    }


def _inspect_zip(path: Path, wanted: str | None, maximum: int, characters: int) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        result: dict[str, Any] = {
            "format": "zip",
            "entry_count": len(infos),
            "entries_returned": min(len(infos), maximum),
            "entries_truncated": len(infos) > maximum,
            "entries": [
                _entry(
                    info.filename, info.file_size, info.compress_size,
                    "directory" if info.is_dir() else "file",
                    _zip_time(info.date_time),
                )
                for info in infos[:maximum]
            ],
        }
        if wanted is not None:
            info = next((candidate for candidate in infos if candidate.filename == wanted), None)
            if info is None:
                raise ArchiveError(f"Archive has no member named {wanted!r}.")
            if info.is_dir():
                raise ArchiveError(f"Archive member {wanted!r} is a directory, not a text file.")
            if info.flag_bits & 0x1:
                raise ArchiveError(f"Archive member {wanted!r} is encrypted and cannot be previewed.")
            with archive.open(info, "r") as stream:
                result["member_preview"] = {"name": wanted, **_preview(stream, characters)}
        return result


def _inspect_tar(path: Path, wanted: str | None, maximum: int, characters: int) -> dict[str, Any]:
    with tarfile.open(path, "r:*") as archive:
        infos = archive.getmembers()
        result: dict[str, Any] = {
            "format": "tar",
            "entry_count": len(infos),
            "entries_returned": min(len(infos), maximum),
            "entries_truncated": len(infos) > maximum,
            "entries": [
                _entry(info.name, info.size, None, _tar_kind(info), _tar_time(info.mtime))
                for info in infos[:maximum]
            ],
        }
        if wanted is not None:
            info = next((candidate for candidate in infos if candidate.name == wanted), None)
            if info is None:
                raise ArchiveError(f"Archive has no member named {wanted!r}.")
            if not info.isfile():
                raise ArchiveError(f"Archive member {wanted!r} is not a regular file.")
            stream = archive.extractfile(info)
            if stream is None:
                raise ArchiveError(f"Archive member {wanted!r} could not be read.")
            with stream:
                result["member_preview"] = {"name": wanted, **_preview(stream, characters)}
        return result


def _zip_time(value: tuple[int, int, int, int, int, int]) -> str | None:
    try:
        return datetime(*value, tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _tar_time(value: float) -> str | None:
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _tar_kind(info: tarfile.TarInfo) -> str:
    if info.isdir():
        return "directory"
    if info.isfile():
        return "file"
    if info.issym():
        return "symlink"
    if info.islnk():
        return "hardlink"
    return "other"
