"""Bounded, link-free inspection and extraction of common archive evidence.

Archives are containers rather than executable inputs.  This module never runs
archive members and deliberately rejects the member types that would make an
extraction escape its requested destination (links, device files, and paths
outside the destination).  Limits apply before output is written, so a task
can examine a potentially hostile attachment without filling its workspace.
"""

from __future__ import annotations

import json
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_MEMBERS = 2_000
_MAX_EXTRACT_MEMBERS = 500
_MAX_EXTRACT_BYTES = 128 * 1024 * 1024


class ArchiveError(Exception):
    """The supplied archive is unsupported, unsafe, or exceeds a limit."""


def inspect_archive(path: Path, *, max_members: int = 100) -> str:
    """Return a compact JSON inventory without opening member payloads."""
    _source_ok(path)
    if not 1 <= max_members <= _MAX_MEMBERS:
        raise ArchiveError(f"max_members must be a whole number from 1 to {_MAX_MEMBERS}.")
    kind, members = _members(path)
    total = sum(size for _, size, _ in members)
    preview = [
        {"path": name, "size_bytes": size, "type": member_type}
        for name, size, member_type in members[:max_members]
    ]
    return json.dumps(
        {
            "format": kind,
            "member_count": len(members),
            "uncompressed_size_bytes": total,
            "members": preview,
            "truncated": len(members) > max_members,
        },
        ensure_ascii=False,
        indent=2,
    )


def extract_archive(path: Path, destination: Path, *, member: str | None = None) -> str:
    """Extract safe regular files into *destination*, subject to fixed quotas."""
    _source_ok(path)
    kind, members = _members(path)
    wanted = members if member is None else [item for item in members if item[0] == member]
    if member is not None and not wanted:
        raise ArchiveError(f"archive has no member named {member!r}.")
    if len(wanted) > _MAX_EXTRACT_MEMBERS:
        raise ArchiveError(f"refusing to extract more than {_MAX_EXTRACT_MEMBERS} members; choose member.")
    unsafe = [name for name, _, member_type in wanted if not _safe_name(name) or member_type not in {"file", "directory"}]
    if unsafe:
        raise ArchiveError("refusing unsafe archive member: " + unsafe[0])
    total = sum(size for _, size, member_type in wanted if member_type == "file")
    if total > _MAX_EXTRACT_BYTES:
        raise ArchiveError(f"refusing to write more than {_MAX_EXTRACT_BYTES} bytes; choose member.")
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    if kind == "zip":
        with zipfile.ZipFile(path) as archive:
            for name, _, member_type in wanted:
                if member_type == "directory":
                    _target(root, name).mkdir(parents=True, exist_ok=True)
                    continue
                target = _target(root, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=64 * 1024)
    else:
        with tarfile.open(path, mode="r:*") as archive:
            for name, _, member_type in wanted:
                target = _target(root, name)
                if member_type == "directory":
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(name)
                if source is None:
                    raise ArchiveError(f"could not read archive member {name!r}.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=64 * 1024)
    return f"Extracted {len(wanted)} member(s) ({total} bytes) from {kind} archive to {destination}."


def _source_ok(path: Path) -> None:
    try:
        if not path.is_file():
            raise ArchiveError("archive path is not a regular file.")
        if path.stat().st_size > _MAX_ARCHIVE_BYTES:
            raise ArchiveError(f"archive exceeds the {_MAX_ARCHIVE_BYTES}-byte inspection limit.")
    except OSError as error:
        raise ArchiveError(f"could not read archive: {error}") from error


def _members(path: Path) -> tuple[str, list[tuple[str, int, str]]]:
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                _member_count_ok(len(infos))
                return "zip", [(info.filename, info.file_size, "directory" if info.is_dir() else "other" if stat.S_ISLNK(info.external_attr >> 16) else "file") for info in infos]
        if tarfile.is_tarfile(path):
            with tarfile.open(path, mode="r:*") as archive:
                infos = archive.getmembers()
                _member_count_ok(len(infos))
                return "tar", [
                    (info.name, info.size, "file" if info.isfile() else "directory" if info.isdir() else "other")
                    for info in infos
                ]
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise ArchiveError(f"invalid or unreadable archive: {error}") from error
    raise ArchiveError("unsupported archive; supported formats are ZIP and TAR (including .tar.gz/.bz2/.xz).")


def _member_count_ok(count: int) -> None:
    if count > _MAX_MEMBERS:
        raise ArchiveError(f"archive has more than {_MAX_MEMBERS} members.")


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and not any(part in {"", "."} for part in path.parts)


def _target(root: Path, name: str) -> Path:
    target = (root / PurePosixPath(name)).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ArchiveError(f"archive member leaves destination: {name}") from error
    return target
