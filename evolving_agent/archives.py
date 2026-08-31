"""Bounded, safe inspection and extraction of task archives.

Archives are often the only form in which a task supplies a multi-file project.
This module accepts already-contained source and destination paths; it never
interprets an archive member as an operating-system path without validating it.
"""
from __future__ import annotations

import gzip
import io
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
_MAX_ENTRIES = 2_000
_MAX_MEMBER_BYTES = 50 * 1024 * 1024
_MAX_TOTAL_BYTES = 150 * 1024 * 1024
_MAX_NESTED_DEPTH = 3


class ArchiveError(Exception):
    """An archive is unsupported, malformed, or exceeds a safe bound."""


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    size: int
    is_dir: bool
    is_link: bool = False


def inspect_archive(path: Path, *, max_entries: int = 200, max_depth: int = 2) -> str:
    """Describe an archive and bounded nested archives without extracting it."""
    _check_source(path)
    if not 1 <= max_entries <= 1_000:
        raise ArchiveError("max_entries must be between 1 and 1000.")
    if not 0 <= max_depth <= _MAX_NESTED_DEPTH:
        raise ArchiveError(f"max_depth must be between 0 and {_MAX_NESTED_DEPTH}.")
    kind, members = _members_from_path(path)
    _validate_members(members)
    lines = [f"{kind} archive: {path.name}", f"Members: {len(members)}"]
    for member in members[:max_entries]:
        suffix = "/" if member.is_dir and not member.name.endswith("/") else ""
        unsafe = " [link omitted]" if member.is_link else ""
        lines.append(f"{member.name}{suffix} ({member.size} bytes){unsafe}")
    if len(members) > max_entries:
        lines.append(f"... [{len(members) - max_entries} more members omitted]")
    if max_depth:
        lines.extend(_nested_lines(path, max_depth, prefix="  "))
    return "\n".join(lines)


def extract_archive(path: Path, destination: Path, *, members: list[str] | None = None) -> str:
    """Extract regular files into *destination*, refusing links and path escapes."""
    _check_source(path)
    kind, listed = _members_from_path(path)
    _validate_members(listed)
    requested = None
    if members is not None:
        if not members or not all(isinstance(name, str) and name for name in members):
            raise ArchiveError("members must be a non-empty list of member names.")
        requested = set(members)
        known = {member.name for member in listed}
        missing = requested - known
        if missing:
            raise ArchiveError(f"Archive has no member {sorted(missing)[0]!r}.")
    destination = destination.resolve()
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ArchiveError(f"Could not create extraction destination: {error}") from error
    written = 0
    count = 0
    try:
        for member, content in _contents_from_path(path, kind):
            if requested is not None and member.name not in requested:
                continue
            if member.is_link:
                raise ArchiveError(f"Refusing link member {member.name!r}.")
            target = _target(destination, member.name)
            if member.is_dir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                output.write(content)
            written += len(content)
            count += 1
    except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError) as error:
        raise ArchiveError(f"Could not extract archive: {error}") from error
    return f"Extracted {count} files ({written} bytes) from {kind} archive to {destination}."


def _check_source(path: Path) -> None:
    if not path.is_file():
        raise ArchiveError(f"{path.name!r} is not a file.")
    if path.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ArchiveError(f"{path.name!r} exceeds the 200 MiB archive limit.")


def _members_from_path(path: Path) -> tuple[str, list[ArchiveMember]]:
    try:
        with path.open("rb") as source:
            return _members_from_stream(source, path.name)
    except (OSError, EOFError, gzip.BadGzipFile, tarfile.TarError, zipfile.BadZipFile) as error:
        raise ArchiveError(f"{path.name!r} is not a readable supported archive: {error}") from error


def _members_from_stream(source: io.BufferedIOBase | io.BytesIO, label: str) -> tuple[str, list[ArchiveMember]]:
    if zipfile.is_zipfile(source):
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            return "ZIP", [ArchiveMember(info.filename, info.file_size, info.is_dir(), _zip_link(info)) for info in archive.infolist()]
    source.seek(0)
    try:
        with tarfile.open(fileobj=source, mode="r:*") as archive:
            return "TAR", [ArchiveMember(info.name, info.size, info.isdir(), info.issym() or info.islnk() or (not info.isfile() and not info.isdir())) for info in archive.getmembers()]
    except (tarfile.TarError, EOFError):
        pass
    source.seek(0)
    try:
        with gzip.GzipFile(fileobj=source) as compressed:
            data = compressed.read(_MAX_MEMBER_BYTES + 1)
    except OSError as error:
        raise ArchiveError(f"{label!r} is not a supported ZIP, TAR, or GZIP archive.") from error
    if len(data) > _MAX_MEMBER_BYTES:
        raise ArchiveError("GZIP member exceeds the 50 MiB extraction limit.")
    name = Path(label).name.removesuffix(".gz") or "unpacked"
    return "GZIP", [ArchiveMember(name, len(data), False)]


def _validate_members(members: list[ArchiveMember]) -> None:
    if len(members) > _MAX_ENTRIES:
        raise ArchiveError(f"Archive contains more than {_MAX_ENTRIES} members.")
    total = 0
    seen: set[str] = set()
    for member in members:
        _safe_name(member.name)
        if member.name in seen:
            raise ArchiveError(f"Archive contains duplicate member {member.name!r}.")
        seen.add(member.name)
        if member.size < 0 or member.size > _MAX_MEMBER_BYTES:
            raise ArchiveError(f"Archive member {member.name!r} exceeds the 50 MiB limit.")
        total += member.size
        if total > _MAX_TOTAL_BYTES:
            raise ArchiveError("Archive expands to more than the 150 MiB total limit.")


def _safe_name(name: str) -> PurePosixPath:
    candidate = PurePosixPath(name)
    if not name or "\\" in name or candidate.is_absolute() or ".." in candidate.parts:
        raise ArchiveError(f"Unsafe archive member path {name!r}.")
    return candidate


def _target(destination: Path, name: str) -> Path:
    target = (destination / _safe_name(name)).resolve()
    if target != destination and destination not in target.parents:
        raise ArchiveError(f"Archive member {name!r} would leave the destination.")
    return target


def _zip_link(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _contents_from_path(path: Path, kind: str):
    if kind == "ZIP":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                member = ArchiveMember(info.filename, info.file_size, info.is_dir(), _zip_link(info))
                yield member, b"" if member.is_dir else _read_limited(archive.open(info), member)
    elif kind == "TAR":
        with tarfile.open(path, mode="r:*") as archive:
            for info in archive.getmembers():
                member = ArchiveMember(info.name, info.size, info.isdir(), info.issym() or info.islnk() or (not info.isfile() and not info.isdir()))
                stream = archive.extractfile(info) if info.isfile() else None
                yield member, b"" if stream is None else _read_limited(stream, member)
    else:
        with gzip.open(path, "rb") as source:
            content = _read_limited(source, ArchiveMember(Path(path.name).name.removesuffix(".gz") or "unpacked", 0, False))
        yield ArchiveMember(Path(path.name).name.removesuffix(".gz") or "unpacked", len(content), False), content


def _read_limited(source, member: ArchiveMember) -> bytes:
    with source:
        content = source.read(_MAX_MEMBER_BYTES + 1)
    if len(content) > _MAX_MEMBER_BYTES:
        raise ArchiveError(f"Archive member {member.name!r} exceeds the 50 MiB limit.")
    return content


def _nested_lines(path: Path, depth: int, *, prefix: str) -> list[str]:
    kind, _ = _members_from_path(path)
    return _nested_from_contents(_contents_from_path(path, kind), depth, prefix)


def _nested_from_contents(contents, depth: int, prefix: str) -> list[str]:
    lines: list[str] = []
    for member, content in contents:
        if member.is_dir or member.is_link or len(content) > _MAX_ARCHIVE_BYTES:
            continue
        try:
            nested_kind, nested = _members_from_stream(io.BytesIO(content), member.name)
            _validate_members(nested)
        except ArchiveError:
            continue
        lines.append(f"{prefix}{member.name}: nested {nested_kind} ({len(nested)} members)")
        for child in nested[:20]:
            lines.append(f"{prefix}  {child.name} ({child.size} bytes)")
        if len(nested) > 20:
            lines.append(f"{prefix}  ... [{len(nested) - 20} more members omitted]")
        if depth > 1:
            lines.extend(_nested_from_bytes(content, member.name, depth - 1, prefix + "  "))
    return lines


def _nested_from_bytes(content: bytes, label: str, depth: int, prefix: str) -> list[str]:
    try:
        kind, _ = _members_from_stream(io.BytesIO(content), label)
        return _nested_from_contents(_contents_from_bytes(content, kind, label), depth, prefix)
    except ArchiveError:
        return []


def _contents_from_bytes(content: bytes, kind: str, label: str):
    if kind == "ZIP":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                member = ArchiveMember(info.filename, info.file_size, info.is_dir(), _zip_link(info))
                yield member, b"" if member.is_dir else _read_limited(archive.open(info), member)
    elif kind == "TAR":
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
            for info in archive.getmembers():
                member = ArchiveMember(info.name, info.size, info.isdir(), info.issym() or info.islnk() or (not info.isfile() and not info.isdir()))
                stream = archive.extractfile(info) if info.isfile() else None
                yield member, b"" if stream is None else _read_limited(stream, member)
    else:
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as source:
            payload = _read_limited(source, ArchiveMember(Path(label).name.removesuffix(".gz") or "unpacked", 0, False))
        yield ArchiveMember(Path(label).name.removesuffix(".gz") or "unpacked", len(payload), False), payload
