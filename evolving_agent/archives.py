"""Bounded read-only inspection of common evidence bundles, including 7Z."""

from __future__ import annotations

import bz2
import gzip
import lzma
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import py7zr
from py7zr import exceptions as py7zr_exceptions

MAX_MEMBERS = 500
MAX_MEMBER_BYTES = 256_000
MAX_LIST_CHARACTERS = 30_000


class ArchiveError(Exception):
    """An archive cannot be identified, listed, or safely read."""


@dataclass(frozen=True)
class ArchiveMember:
    """Metadata needed to show one regular member without extracting it."""

    name: str
    size: int


def inspect_archive(path: Path, member: str | None = None) -> str:
    """List an archive or return a bounded UTF-8 preview of one member.

    Archive members are never written to disk. A requested member must be an
    exact regular-file name, and its declared uncompressed size is checked
    before it is opened. This includes 7Z archives, whose decoder otherwise
    returns an in-memory object for a selected member.
    """
    try:
        if zipfile.is_zipfile(path):
            return _zip(path, member)
        if tarfile.is_tarfile(path):
            return _tar(path, member)
        if _has_magic(path, b"7z\xbc\xaf'\x1c"):
            return _seven_zip(path, member)
        if _has_magic(path, b"\x1f\x8b"):
            return _single_stream(path, member, "GZIP", ".gz", gzip.open)
        if _has_magic(path, b"BZh"):
            return _single_stream(path, member, "BZIP2", ".bz2", bz2.open)
        if _has_magic(path, b"\xfd7zXZ\x00"):
            return _single_stream(path, member, "XZ", ".xz", lzma.open)
    except (
        OSError, EOFError, zipfile.BadZipFile, tarfile.TarError, lzma.LZMAError,
        py7zr_exceptions.ArchiveError, py7zr_exceptions.Bad7zFile,
        py7zr_exceptions.CrcError, py7zr_exceptions.DecompressionError,
        py7zr_exceptions.PasswordRequired, py7zr_exceptions.UnsupportedCompressionMethodError,
    ) as error:
        raise ArchiveError(f"Could not inspect archive: {error}") from error
    raise ArchiveError("Supported archive formats are ZIP, TAR (including compressed TAR), 7Z, GZIP, BZIP2, and XZ.")


def _zip(path: Path, requested: str | None) -> str:
    with zipfile.ZipFile(path) as archive:
        members = [ArchiveMember(info.filename, info.file_size) for info in archive.infolist() if not info.is_dir() and not _zip_symlink(info)]
        if requested is None:
            return _listing("ZIP", members)
        info = next((item for item in archive.infolist() if item.filename == requested), None)
        if info is None or info.is_dir() or _zip_symlink(info):
            raise ArchiveError(f"No readable regular member named {requested!r}.")
        _check_size(info.file_size, requested)
        with archive.open(info) as stream:
            return _preview(requested, stream.read(MAX_MEMBER_BYTES + 1))


def _tar(path: Path, requested: str | None) -> str:
    with tarfile.open(path, "r:*") as archive:
        members = [ArchiveMember(info.name, info.size) for info in archive if info.isfile()]
        if requested is None:
            return _listing("TAR", members)
        info = next((item for item in members if item.name == requested), None)
        if info is None:
            raise ArchiveError(f"No readable regular member named {requested!r}.")
        _check_size(info.size, requested)
        source = archive.extractfile(requested)
        if source is None:
            raise ArchiveError(f"Could not open regular member {requested!r}.")
        with source:
            return _preview(requested, source.read(MAX_MEMBER_BYTES + 1))


def _seven_zip(path: Path, requested: str | None) -> str:
    """Inspect a 7Z member in memory after a strict declared-size check."""
    with py7zr.SevenZipFile(path, mode="r") as archive:
        infos = archive.list()
        members = [ArchiveMember(info.filename, info.uncompressed) for info in infos if not info.is_directory]
        if requested is None:
            return _listing("7Z", members)
        selected = next((item for item in members if item.name == requested), None)
        if selected is None:
            raise ArchiveError(f"No readable regular member named {requested!r}.")
        _check_size(selected.size, requested)
        contents = archive.read([requested])
        source = contents.get(requested)
        if source is None:
            raise ArchiveError(f"Could not open regular member {requested!r}.")
        return _preview(requested, source.read(MAX_MEMBER_BYTES + 1))


def _single_stream(path: Path, requested: str | None, kind: str, suffix: str, opener: object) -> str:
    """Preview an anonymous compressed stream without materializing it."""
    name = path.name.removesuffix(suffix) or f"{kind.lower()}-payload"
    if requested is not None and requested != name:
        raise ArchiveError(f"{kind} has one member named {name!r}.")
    if requested is None:
        return _listing(kind, [ArchiveMember(name, -1)])
    with opener(path, "rb") as stream:  # type: ignore[operator]
        return _preview(name, stream.read(MAX_MEMBER_BYTES + 1))


def _listing(kind: str, members: list[ArchiveMember]) -> str:
    shown = members[:MAX_MEMBERS]
    lines = [f"{kind} archive: {len(members)} readable file member(s)."]
    for item in shown:
        size = "unknown size" if item.size < 0 else f"{item.size} bytes"
        lines.append(f"{item.name} ({size})")
    if len(members) > len(shown):
        lines.append(f"... [{len(members) - len(shown)} more members omitted]")
    result = "\n".join(lines)
    return result[:MAX_LIST_CHARACTERS] + ("\n... [listing truncated]" if len(result) > MAX_LIST_CHARACTERS else "")


def _preview(name: str, data: bytes) -> str:
    truncated = len(data) > MAX_MEMBER_BYTES
    data = data[:MAX_MEMBER_BYTES]
    text = data.decode("utf-8", errors="replace")
    suffix = "\n... [member preview truncated]" if truncated else ""
    return f"{name} ({len(data)} bytes preview)\n---\n{text}{suffix}"


def _check_size(size: int, name: str) -> None:
    if size > MAX_MEMBER_BYTES:
        raise ArchiveError(f"{name!r} is {size} bytes, above the {MAX_MEMBER_BYTES}-byte member preview limit.")


def _zip_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _has_magic(path: Path, magic: bytes) -> bool:
    try:
        with path.open("rb") as source:
            return source.read(len(magic)) == magic
    except OSError as error:
        raise ArchiveError(f"Could not read archive: {error}") from error
