"""Bounded, path-safe inspection and extraction of task archives."""

from __future__ import annotations

import shutil
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

_MAX_INPUT_BYTES = 80_000_000
_MAX_MEMBERS = 500
_MAX_MEMBER_BYTES = 12_000_000
_MAX_EXTRACTED_BYTES = 40_000_000
_COPY_CHUNK_BYTES = 64 * 1024
_SUPPORTED = ".zip, .tar, .tar.gz, .tgz, .tar.bz2, .tbz2, .tar.xz, .txz"


class ArchiveError(Exception):
    """An archive cannot safely be inspected or extracted."""


@dataclass(frozen=True)
class ArchiveMember:
    """A regular archive member that can be extracted."""

    name: str
    size: int


def list_archive(path: Path) -> tuple[ArchiveMember, ...]:
    """Return bounded regular-file inventory, refusing unsafe metadata."""
    return tuple(_members(path))


def format_inventory(path: Path) -> str:
    """Render an archive inventory in a concise model-readable form."""
    members = list_archive(path)
    header = f"Archive: {path.name}\nExtractable files: {len(members)}"
    if not members:
        return header + "\n[No regular files.]"
    return header + "\n" + "\n".join(f"{item.name} ({item.size} bytes)" for item in members)


def extract_archive(path: Path, destination: Path, *, member: str | None = None) -> tuple[ArchiveMember, ...]:
    """Extract one named file or all files beneath an already-confined destination.

    Metadata is fully inspected before any output is written. Archive paths are
    validated as portable relative paths, and links/devices are never extracted.
    """
    items = list(_members(path))
    if member is not None:
        selected = [item for item in items if item.name == member]
        if not selected:
            raise ArchiveError(f"Archive has no extractable file named {member!r}.")
    else:
        selected = items
    total = sum(item.size for item in selected)
    if total > _MAX_EXTRACTED_BYTES:
        raise ArchiveError(f"Selected files total {total:,} bytes; limit is {_MAX_EXTRACTED_BYTES:,} bytes.")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        if _is_zip(path):
            with zipfile.ZipFile(path) as archive:
                for item in selected:
                    with archive.open(item.name) as source:
                        _copy_to_destination(source, destination, item)
        else:
            with tarfile.open(path, "r:*") as archive:
                for item in selected:
                    source = archive.extractfile(item.name)
                    if source is None:
                        raise ArchiveError(f"Could not read {item.name!r} from archive.")
                    with source:
                        _copy_to_destination(source, destination, item)
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise ArchiveError(f"Could not extract archive: {error}") from error
    return tuple(selected)


def _members(path: Path) -> Iterator[ArchiveMember]:
    _check_input(path)
    try:
        if _is_zip(path):
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > _MAX_MEMBERS:
                    raise ArchiveError(f"Archive has more than {_MAX_MEMBERS} members.")
                for info in infos:
                    if info.is_dir():
                        continue
                    if info.flag_bits & 0x1:
                        raise ArchiveError(f"{info.filename!r} is encrypted and cannot be read.")
                    # Unix symlink bits in ZIP external attributes.  Such a
                    # member must not turn a task archive into a path escape.
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ArchiveError(f"{info.filename!r} is a symbolic link and cannot be extracted.")
                    yield ArchiveMember(_safe_name(info.filename), _safe_size(info.file_size, info.filename))
        else:
            with tarfile.open(path, "r:*") as archive:
                infos = archive.getmembers()
                if len(infos) > _MAX_MEMBERS:
                    raise ArchiveError(f"Archive has more than {_MAX_MEMBERS} members.")
                for info in infos:
                    if info.isdir():
                        continue
                    if not info.isfile():
                        raise ArchiveError(f"{info.name!r} is not a regular file and cannot be extracted.")
                    yield ArchiveMember(_safe_name(info.name), _safe_size(info.size, info.name))
    except ArchiveError:
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise ArchiveError(f"Could not read archive: {error}") from error


def _check_input(path: Path) -> None:
    if not path.is_file():
        raise ArchiveError(f"{path.name!r} is not a file.")
    if path.stat().st_size > _MAX_INPUT_BYTES:
        raise ArchiveError(f"{path.name!r} is larger than {_MAX_INPUT_BYTES:,} bytes.")
    if not _is_zip(path) and not tarfile.is_tarfile(path):
        raise ArchiveError(f"Supported archive formats are {_SUPPORTED}.")


def _is_zip(path: Path) -> bool:
    return zipfile.is_zipfile(path)


def _safe_name(name: str) -> str:
    portable = name.replace("\\", "/")
    parts = PurePosixPath(portable).parts
    if not name or portable.startswith("/") or ".." in parts:
        raise ArchiveError(f"Unsafe archive member path {name!r}.")
    cleaned = "/".join(part for part in parts if part not in (".", "/"))
    if not cleaned:
        raise ArchiveError(f"Unsafe archive member path {name!r}.")
    return cleaned


def _safe_size(size: int, name: str) -> int:
    if size < 0 or size > _MAX_MEMBER_BYTES:
        raise ArchiveError(f"{name!r} is {size:,} bytes; per-file limit is {_MAX_MEMBER_BYTES:,} bytes.")
    return size


def _copy_to_destination(source: BinaryIO, destination: Path, item: ArchiveMember) -> None:
    target = destination.joinpath(*PurePosixPath(item.name).parts)
    # Names were validated above. resolve remains a defense against a directory
    # symlink pre-existing at the requested destination.
    root = destination.resolve()
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise ArchiveError(f"{item.name!r} would leave the extraction destination.")
    if target.exists() and target.is_dir():
        raise ArchiveError(f"{item.name!r} conflicts with an existing directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as output:
        shutil.copyfileobj(source, output, length=_COPY_CHUNK_BYTES)
    if target.stat().st_size != item.size:
        raise ArchiveError(f"{item.name!r} changed size while extracting.")
