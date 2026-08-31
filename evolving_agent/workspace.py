"""The one directory this agent may read and write.

Every file operation the model asks for goes through this class, and every path
it names is resolved against one root and refused if it leaves it. That is the
whole containment story: the tools cannot be talked into touching the agent's
own image, and a model that asks for ``../../etc/passwd`` gets an error message
rather than a file.

Walking the tree obeys the same boundary. A symbolic link is never followed and
never listed, so a link a command dropped in the workspace cannot make this
class read, hash or publish a byte from outside it.
"""

import hashlib
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_PARENT_SEGMENT = ".."

_MAX_READ_BYTES = 64_000
"""How much of one file a read returns, so a large file cannot fill the context."""

_MAX_LISTED_FILES = 500
"""How many paths one listing returns, so a large tree cannot fill the context."""

_SKIPPED_DIRECTORY_NAMES = frozenset({"__pycache__", ".git"})
"""Directories that are never copied into a workspace and never listed.

Interpreter bytecode and version-control metadata are not source: copied into a
successor they would be published as part of it.
"""

_BYTECODE_SUFFIXES = frozenset({".pyc", ".pyo"})


class WorkspaceError(Exception):
    """A file operation this workspace will not perform."""


@dataclass(frozen=True)
class FileEntry:
    """One file of the workspace, as a listing reports it."""

    relative_path: str
    byte_size: int


class Workspace:
    """A contained directory tree: the only files this agent can touch."""

    def __init__(self, root: Path) -> None:
        """Hold the root every path is resolved against.

        Args:
            root: Directory the workspace lives in. It need not exist yet;
                :meth:`prepare` creates it.

        """
        self._root = root

    @property
    def root(self) -> Path:
        """Return the directory this workspace is rooted at."""
        return self._root

    def prepare(self) -> None:
        """Make sure the root exists and is a directory.

        Raises:
            WorkspaceError: If the root exists as something other than a
                directory, or cannot be created.

        """
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as unusable:
            raise WorkspaceError(
                f"The workspace {self._root} cannot be used: {unusable}"
            ) from unusable
        if not self._root.is_dir():
            raise WorkspaceError(f"The workspace {self._root} is not a directory.")

    def resolve(self, relative_path: str) -> Path:
        """Return the absolute path one relative path names inside this tree.

        Args:
            relative_path: Path as the caller supplied it, relative to the
                root.

        Returns:
            The absolute path, guaranteed to lie inside the root.

        Raises:
            WorkspaceError: If the path is blank, absolute, or would leave the
                tree — including by way of a symbolic link.

        """
        wanted = relative_path.strip()
        if not wanted:
            raise WorkspaceError("A path is required.")
        parts = PurePosixPath(wanted).parts
        if wanted.startswith("/") or _PARENT_SEGMENT in parts:
            raise WorkspaceError(
                f"{wanted!r} must be a relative path that stays inside the workspace."
            )
        root = self._root.resolve()
        resolved = (root / wanted).resolve()
        if resolved != root and root not in resolved.parents:
            raise WorkspaceError(
                f"{wanted!r} resolves outside the workspace and will not be used."
            )
        return resolved

    def entries(self) -> tuple[FileEntry, ...]:
        """Return every file in the tree, sorted, bytecode and metadata aside.

        Returns:
            One entry per regular file the workspace actually holds, up to the
            listing limit. Symbolic links are not among them: they are not
            source, and one pointing out of the tree would otherwise be read as
            though it were.

        """
        root = self._root.resolve()
        if not root.is_dir():
            return ()
        found: list[FileEntry] = []
        for path in _contained_paths(root):
            relative = path.relative_to(root)
            if _is_skipped(relative):
                continue
            found.append(
                FileEntry(
                    relative_path=relative.as_posix(),
                    byte_size=path.stat().st_size,
                )
            )
            if len(found) == _MAX_LISTED_FILES:
                break
        return tuple(found)

    def is_empty(self) -> bool:
        """Report whether the tree holds no file worth improving on."""
        return not self.entries()

    def read_text(self, relative_path: str, *, max_bytes: int = _MAX_READ_BYTES) -> str:
        """Return one file's text, truncated to a readable size.

        Args:
            relative_path: The file to read, relative to the root.
            max_bytes: How much of it to return.

        Returns:
            The file's text, with a marker appended when it was truncated.

        Raises:
            WorkspaceError: If the path leaves the tree, names no file, or
                cannot be read.

        """
        path = self.resolve(relative_path)
        if not path.is_file():
            raise WorkspaceError(f"{relative_path!r} is not a file in the workspace.")
        try:
            content = path.read_bytes()
        except OSError as unreadable:
            raise WorkspaceError(
                f"{relative_path!r} could not be read: {unreadable}"
            ) from unreadable
        text = content[:max_bytes].decode("utf-8", errors="replace")
        if len(content) > max_bytes:
            return f"{text}\n... [truncated at {max_bytes} bytes]"
        return text

    def write_text(self, relative_path: str, content: str) -> int:
        """Write one file, creating the directories it needs.

        Args:
            relative_path: The file to write, relative to the root.
            content: The whole new content of that file.

        Returns:
            How many bytes were written.

        Raises:
            WorkspaceError: If the path leaves the tree, the content is not
                text that can be stored, or the file cannot be written.

        """
        path = self.resolve(relative_path)
        # Encoded before anything is opened: a model can ask for text no
        # encoder will take, and writing it directly would truncate the file
        # first and fail second, leaving a successor holding an empty file
        # nobody asked for.
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError as unencodable:
            raise WorkspaceError(
                f"{relative_path!r} could not be written: {unencodable}. Send "
                f"content that is text."
            ) from unencodable
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            written = path.write_bytes(encoded)
        except OSError as unwritable:
            raise WorkspaceError(
                f"{relative_path!r} could not be written: {unwritable}"
            ) from unwritable
        return written

    def delete(self, relative_path: str) -> None:
        """Remove one file or one directory tree from the workspace.

        Args:
            relative_path: What to remove, relative to the root.

        Raises:
            WorkspaceError: If the path leaves the tree, names nothing, or
                names the root itself.

        """
        path = self.resolve(relative_path)
        if path == self._root.resolve():
            raise WorkspaceError("The workspace itself cannot be deleted.")
        if not path.exists():
            raise WorkspaceError(f"{relative_path!r} does not exist in the workspace.")
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as undeletable:
            raise WorkspaceError(
                f"{relative_path!r} could not be removed: {undeletable}"
            ) from undeletable

    def copy_tree_from(self, source: Path) -> int:
        """Copy a source tree in, leaving bytecode and metadata behind.

        Args:
            source: Directory to copy from.

        Returns:
            How many files were copied.

        Raises:
            WorkspaceError: If the source is not a directory, or a file cannot
                be copied.

        """
        origin = source.resolve()
        if not origin.is_dir():
            raise WorkspaceError(f"There is no source tree at {origin}.")
        self.prepare()
        copied = 0
        for path in _contained_paths(origin):
            relative = path.relative_to(origin)
            if _is_skipped(relative):
                continue
            target = self._root / relative
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            except OSError as uncopyable:
                raise WorkspaceError(
                    f"{relative.as_posix()!r} could not be copied into the "
                    f"workspace: {uncopyable}"
                ) from uncopyable
            copied += 1
        return copied

    def clear(self) -> None:
        """Remove everything in the tree, leaving the root in place.

        Raises:
            WorkspaceError: If something in the tree cannot be removed.

        """
        root = self._root.resolve()
        if not root.is_dir():
            return
        for path in sorted(root.iterdir()):
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as undeletable:
                raise WorkspaceError(
                    f"The workspace could not be cleared: {undeletable}"
                ) from undeletable

    def digest(self) -> str:
        """Return one value that changes whenever the tree's content changes.

        Returns:
            A hexadecimal digest over every path and every byte in the tree, so
            a run that changed nothing can be told from one that did.

        """
        running = hashlib.sha256()
        root = self._root.resolve()
        for entry in self.entries():
            running.update(entry.relative_path.encode("utf-8"))
            running.update((root / entry.relative_path).read_bytes())
        return running.hexdigest()


def _is_skipped(relative: Path) -> bool:
    """Report whether one path is metadata rather than source."""
    return (
        any(part in _SKIPPED_DIRECTORY_NAMES for part in relative.parts)
        or relative.suffix in _BYTECODE_SUFFIXES
    )


def _contained_paths(root: Path) -> Iterator[Path]:
    """Yield every file under one root, in order, following no symbolic link.

    A link is neither yielded nor descended into. That is what keeps every walk
    inside the tree it was given: a link to ``/etc`` would otherwise put the
    host's files in a listing and in a digest.

    Args:
        root: Directory to walk.

    Yields:
        Each contained file, parents before their children.

    """
    try:
        children = sorted(root.iterdir())
    except OSError:
        return
    for child in children:
        if child.is_symlink():
            continue
        if child.is_dir():
            yield from _contained_paths(child)
        elif child.is_file():
            yield child
