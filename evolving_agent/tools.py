"""The capabilities this agent has: reading, writing and running things.

The tools are declared here, once, as data — a name, a description and a JSON
Schema for the arguments — and are implemented against the workspace and the
command runner. The MCP server publishes exactly this list, so what a model is
told it can do and what the agent can actually do cannot drift apart.

Every failure is a :class:`ToolFailureError` carrying a sentence the model can act
on. That is the difference between a tool that teaches and one that merely
refuses: "that path leaves the workspace" gets a corrected second attempt, an
unhandled exception ends the run.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolving_agent.archives import ArchiveError, inspect_archive
from evolving_agent.charts import ChartError, create_chart
from evolving_agent.commands import CommandRunner
from evolving_agent.documents import DocumentError, create_document
from evolving_agent.emails import EmailError, inspect_email
from evolving_agent.images import ImageError, create_image, inspect_image
from evolving_agent.office import OfficeError, inspect_office
from evolving_agent.pdfs import PdfError, inspect_pdf
from evolving_agent.pdf_creation import PdfCreationError, create_pdf
from evolving_agent.presentations import PresentationError, create_presentation
from evolving_agent.spreadsheets import SpreadsheetError, inspect_spreadsheet
from evolving_agent.spreadsheet_creation import SpreadsheetCreationError, create_spreadsheet
from evolving_agent.workspace import Workspace, WorkspaceError
from evolving_agent.web import WebError, fetch_url

_MAX_LISTING_CHARACTERS = 8_000

MATERIALS_PREFIX = "materials/"
"""How a path names the read-only input files a task was given."""

OUTPUT_PREFIX = "output/"
"""How a path names the directory a task's deliverables are left in."""


class ToolFailureError(Exception):
    """A tool could not do what it was asked, for a reason worth reporting."""


@dataclass(frozen=True)
class ToolDefinition:
    """One capability, as it is published to a model."""

    name: str
    description: str
    input_schema: dict[str, Any]


class WorkspaceTools:
    """Reads, writes and runs things, all inside one workspace.

    Two further trees may sit beside it. ``materials/`` is what a task handed
    in: readable, listed, never written. ``output/`` is where a task's
    deliverables go: written, listed, collected afterwards. Both are named by a
    path prefix, so the model has one set of tools and three places to point
    them at, and the containment story is the same for each — a path is
    resolved against the tree its prefix names and refused if it leaves it.
    """

    def __init__(
        self,
        workspace: Workspace,
        commands: CommandRunner,
        *,
        materials: Workspace | None = None,
        output: Workspace | None = None,
    ) -> None:
        """Hold the trees the tools work in and the runner they execute through.

        Args:
            workspace: The contained directory every path is resolved against.
            commands: How a command is run, bounded and cleaned.
            materials: The read-only inputs a task was given, if any.
            output: Where a task's deliverables are left, if anywhere.

        """
        self._workspace = workspace
        self._commands = commands
        self._materials = materials
        self._output = output

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return every tool this agent publishes, in a stable order."""
        return (
            ToolDefinition(
                name="list_files",
                description=(
                    "List every file in the workspace with its size in bytes, "
                    "followed by the task's input files under materials/ and "
                    "the deliverables written so far under output/. Takes no "
                    "arguments."
                ),
                input_schema={"type": "object", "properties": {}, "required": []},
            ),
            ToolDefinition(
                name="read_file",
                description=(
                    "Read one file as text. Paths are relative to the workspace; "
                    "a path starting with materials/ reads one of the task's "
                    "input files, and one starting with output/ reads a "
                    "deliverable. Large files are truncated."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workspace root.",
                        }
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="write_file",
                description=(
                    "Write one file, replacing it if it exists and creating "
                    "the directories it needs. The content is the whole new "
                    "file, not a patch. A path starting with output/ writes a "
                    "deliverable the task asked for; materials/ is read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workspace root.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The complete new content of the file.",
                        },
                    },
                    "required": ["path", "content"],
                },
            ),
            ToolDefinition(
                name="delete_path",
                description="Remove one file or directory from the workspace.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workspace root.",
                        }
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="fetch_url",
                description=(
                    "Retrieve a remote HTTP or HTTPS page, text, or JSON response as bounded "
                    "evidence. Follows at most five redirects, does not execute JavaScript, "
                    "and returns URL, status, content type, and body text."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Absolute HTTP or HTTPS URL to retrieve."},
                        "max_characters": {"type": "integer", "description": "Body preview characters, 1 to 20000 (default 12000)."},
                        "timeout_seconds": {"type": "integer", "description": "Request timeout in seconds, 1 to 45 (default 20)."},
                    },
                    "required": ["url"],
                },
            ),
            ToolDefinition(
                name="inspect_spreadsheet",
                description=(
                    "Inspect a CSV, TSV, XLSX, or ODS spreadsheet without executing "
                    "macros, formulas, or links. Returns sheet names and a bounded JSON "
                    "table preview. Use a materials/ path for an input workbook."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Spreadsheet path."},
                        "sheet": {"type": "string", "description": "Optional exact sheet name."},
                        "max_rows": {"type": "integer", "description": "Preview rows, 1 to 500."},
                        "max_columns": {"type": "integer", "description": "Preview columns, 1 to 100."},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="create_spreadsheet",
                description=(
                    "Create an editable XLSX workbook in the workspace or output/ from structured sheets. "
                    "Each sheet can have a styled table (columns and rows) plus individual A1 cells, "
                    "including formulas stored for Excel to calculate when opened."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Destination .xlsx path, usually under output/."},
                        "title": {"type": "string", "description": "Optional workbook title property."},
                        "sheets": {
                            "type": "array", "minItems": 1, "maxItems": 20,
                            "description": "Sheet objects in workbook order.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": "Excel sheet name (1-31 characters)."},
                                    "columns": {"type": "array", "items": {"type": "string"}, "description": "Optional table column headings."},
                                    "rows": {"type": "array", "items": {"type": "array", "items": {}}, "description": "Table rows, each matching columns."},
                                    "cells": {"type": "array", "description": "Optional individual cells or formulas.", "items": {"type": "object", "properties": {"reference": {"type": "string", "description": "A1 reference."}, "value": {"description": "String (including =formula), number, boolean, or null."}, "number_format": {"type": "string"}}, "required": ["reference", "value"]}},
                                },
                                "required": ["name"],
                            },
                        },
                    },
                    "required": ["path", "sheets"],
                },
            ),
            ToolDefinition(
                name="inspect_archive",
                description=(
                    "Inspect a ZIP or TAR archive without extracting it. Returns a bounded "
                    "member inventory and can preview one named text member in place. "
                    "Use a materials/ path for archive evidence."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Archive path."},
                        "member": {"type": "string", "description": "Optional exact archive member name to preview."},
                        "max_entries": {"type": "integer", "description": "Inventory entries, 1 to 500."},
                        "max_characters": {"type": "integer", "description": "Text preview characters, 1 to 12000."},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="inspect_office",
                description=(
                    "Inspect a DOCX or PPTX Office file without opening macros, links, embedded files, "
                    "or media. Returns bounded visible paragraph, table, or slide text. Use a materials/ path."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "DOCX or PPTX file path."},
                        "max_slides": {"type": "integer", "description": "PPTX slides to preview, 1 to 100 (default 10)."},
                        "max_characters": {"type": "integer", "description": "Total JSON preview characters, 1 to 20000 (default 8000)."},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="inspect_pdf",
                description=(
                    "Inspect a PDF's metadata, embedded-file names, and bounded extracted page text without "
                    "rendering it or opening JavaScript, forms, links, or attachments. "
                    "Use a materials/ path for an input PDF."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "PDF file path."},
                        "page": {"type": "integer", "description": "Optional one-based page number."},
                        "max_pages": {"type": "integer", "description": "Pages to preview when page is omitted, 1 to 25."},
                        "max_characters": {"type": "integer", "description": "Text preview characters per page, 1 to 12000."},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="create_pdf",
                description=(
                    "Create a static, print-ready PDF in the workspace or output/ from structured blocks. "
                    "Blocks support headings, paragraphs, bullets, tables, page breaks, and local images; "
                    "the PDF contains no active content."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Destination .pdf path, usually under output/."},
                        "title": {"type": "string", "description": "Optional PDF title metadata and first-page title."},
                        "blocks": {"type": "array", "minItems": 1, "maxItems": 100, "description": "Document blocks in order.", "items": {"type": "object", "properties": {"type": {"type": "string", "enum": ["heading", "paragraph", "bullets", "table", "page_break", "image"]}, "text": {"type": "string"}, "level": {"type": "integer", "description": "Heading level 1 through 6."}, "items": {"type": "array", "items": {"type": "string"}}, "columns": {"type": "array", "items": {"type": "string"}}, "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}, "path": {"type": "string", "description": "Readable local image path."}, "caption": {"type": "string"}}}},
                    },
                    "required": ["path", "blocks"],
                },
            ),
            ToolDefinition(
                name="create_image",
                description=(
                    "Create a PNG or JPEG visual in the workspace or output/ using a bounded "
                    "canvas and declarative rectangle, ellipse, line, text, and local-image "
                    "operations. Coordinates are pixel values; image operations can compose "
                    "readable workspace or materials/ images."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Destination .png, .jpg, or .jpeg path, usually under output/."},
                        "width": {"type": "integer", "description": "Canvas width in pixels, 1 to 6000."},
                        "height": {"type": "integer", "description": "Canvas height in pixels, 1 to 6000."},
                        "background": {"type": "string", "description": "Optional Pillow color, default white."},
                        "operations": {"type": "array", "maxItems": 200, "description": "Drawing operations in order.", "items": {"type": "object", "properties": {"type": {"type": "string", "enum": ["rectangle", "ellipse", "line", "text", "image"]}, "box": {"type": "array", "items": {"type": "integer"}, "description": "[left, top, right, bottom] for shapes and images."}, "fill": {"type": "string"}, "outline": {"type": "string"}, "stroke_width": {"type": "integer"}, "points": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}, "position": {"type": "array", "items": {"type": "integer"}}, "text": {"type": "string"}, "font_size": {"type": "integer"}, "path": {"type": "string", "description": "Readable image path for image operation."}}, "required": ["type"]}}
                    },
                    "required": ["path", "width", "height"],
                },
            ),
            ToolDefinition(
                name="create_chart",
                description=(
                    "Create a polished PNG or JPEG bar, line, or pie chart from one labeled numeric series. "
                    "Useful for quantitative report visuals; writes only to the workspace or output/."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Destination .png, .jpg, or .jpeg path, usually under output/."},
                        "chart_type": {"type": "string", "enum": ["bar", "line", "pie"]},
                        "labels": {"type": "array", "minItems": 1, "maxItems": 60, "items": {"type": "string"}},
                        "values": {"type": "array", "minItems": 1, "maxItems": 60, "items": {"type": "number"}},
                        "width": {"type": "integer", "description": "Optional width in pixels, 300 to 6000; default 1200."},
                        "height": {"type": "integer", "description": "Optional height in pixels, 250 to 6000; default 800."},
                        "title": {"type": "string", "description": "Optional chart title."},
                        "colors": {"type": "array", "description": "Optional Pillow color strings, exactly one per value.", "items": {"type": "string"}}
                    },
                    "required": ["path", "chart_type", "labels", "values"]
                },
            ),
            ToolDefinition(
                name="inspect_image",
                description=(
                    "Inspect a PNG, JPEG, WebP, TIFF, BMP, or GIF image locally. "
                    "Returns dimensions, frame count, printable EXIF metadata, and bounded "
                    "English OCR text without rendering the image. Use a materials/ path for evidence."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Image file path."},
                        "ocr": {"type": "boolean", "description": "Whether to extract English OCR text (default true)."},
                        "max_characters": {"type": "integer", "description": "OCR preview characters, 1 to 20000."},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="inspect_email",
                description=(
                    "Inspect an EML, EMLX, or MBOX email evidence file without "
                    "opening attachments or rendering HTML. Returns decoded selected "
                    "headers, bounded body text, and attachment metadata. For MBOX, "
                    "message_index is zero-based. Use a materials/ path for task input."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Email file path."},
                        "message_index": {"type": "integer", "description": "MBOX message index, starting at 0."},
                        "max_messages": {"type": "integer", "description": "MBOX summaries to list, 1 to 50."},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="create_document",
                description=(
                    "Create an editable Word DOCX document in the workspace or output/ from "
                    "structured blocks. Blocks can be headings, paragraphs, bullet lists, "
                    "tables, page breaks, and local images from the readable workspace or "
                    "materials/. This creates a file; it does not render or execute content."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Destination .docx path, usually under output/."},
                        "title": {"type": "string", "description": "Optional document title and first heading."},
                        "blocks": {
                            "type": "array", "minItems": 1, "maxItems": 100,
                            "description": "Document block objects, in order.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string", "enum": ["heading", "paragraph", "bullets", "table", "page_break", "image"]},
                                    "text": {"type": "string", "description": "Text for heading or paragraph blocks."},
                                    "level": {"type": "integer", "description": "Heading level, 1 through 9 (default 1)."},
                                    "items": {"type": "array", "items": {"type": "string"}, "description": "Bullet text."},
                                    "columns": {"type": "array", "items": {"type": "string"}, "description": "Table header text."},
                                    "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}, "description": "Table rows."},
                                    "path": {"type": "string", "description": "Image path, e.g. materials/chart.png."},
                                    "caption": {"type": "string", "description": "Optional image caption."},
                                },
                                "required": ["type"],
                            },
                        },
                    },
                    "required": ["path", "blocks"],
                },
            ),
            ToolDefinition(
                name="create_presentation",
                description=(
                    "Create an editable PPTX presentation in the workspace or output/ from "
                    "structured slides. Each slide may have title, bullets, a table with "
                    "columns and rows, and/or an image path from the readable workspace or "
                    "materials/. This creates a file; it does not render or execute content."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Destination .pptx path, usually under output/."},
                        "slides": {
                            "type": "array", "minItems": 1, "maxItems": 30,
                            "description": "Slide objects with optional title, bullets, image, and table.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "bullets": {"type": "array", "items": {"type": "string"}},
                                    "image": {"type": "string", "description": "Readable image path, e.g. materials/chart.png."},
                                    "table": {"type": "object", "properties": {"columns": {"type": "array", "items": {"type": "string"}}, "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}}},
                                },
                            },
                        },
                    },
                    "required": ["path", "slides"],
                },
            ),
            ToolDefinition(
                name="run_command",
                description=(
                    "Run one command in the workspace and return its exit "
                    "code and output. There is no shell: give the program and "
                    'its arguments as a list, e.g. ["python", "-m", '
                    '"unittest", "discover"]. The container has no network '
                    "unless the deployment granted one."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The program and its arguments.",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": (
                                "How long the command may run before it is stopped."
                            ),
                        },
                    },
                    "required": ["command"],
                },
            ),
        )

    def call(self, name: str, arguments: Mapping[str, Any]) -> str:
        """Perform one published tool call and return what it produced.

        Args:
            name: Name of the tool, as published.
            arguments: The arguments the caller supplied.

        Returns:
            What the tool produced, as the text a model reads.

        Raises:
            ToolFailureError: If the tool is unknown, an argument is missing
                or of the wrong shape, or the operation itself was refused.

        """
        handlers = {
            "list_files": self._list_files,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "delete_path": self._delete_path,
            "fetch_url": self._fetch_url,
            "inspect_spreadsheet": self._inspect_spreadsheet,
            "create_spreadsheet": self._create_spreadsheet,
            "inspect_archive": self._inspect_archive,
            "inspect_email": self._inspect_email,
            "inspect_office": self._inspect_office,
            "inspect_pdf": self._inspect_pdf,
            "create_pdf": self._create_pdf,
            "create_image": self._create_image,
            "create_chart": self._create_chart,
            "inspect_image": self._inspect_image,
            "create_document": self._create_document,
            "create_presentation": self._create_presentation,
            "run_command": self._run_command,
        }
        handler = handlers.get(name)
        if handler is None:
            published = ", ".join(sorted(handlers))
            raise ToolFailureError(
                f"There is no tool called {name!r}. Tools: {published}."
            )
        try:
            return handler(arguments)
        except WorkspaceError as refused:
            raise ToolFailureError(str(refused)) from refused

    def _list_files(self, arguments: Mapping[str, Any]) -> str:
        """Return every tree's files, one per line, with their sizes."""
        del arguments
        lines = [
            f"{entry.relative_path} ({entry.byte_size} bytes)"
            for entry in self._workspace.entries()
        ]
        for prefix, tree in (
            (MATERIALS_PREFIX, self._materials),
            (OUTPUT_PREFIX, self._output),
        ):
            if tree is None:
                continue
            lines.extend(
                f"{prefix}{entry.relative_path} ({entry.byte_size} bytes)"
                for entry in tree.entries()
            )
        if not lines:
            return "The workspace is empty."
        listing = "\n".join(lines)
        if len(listing) <= _MAX_LISTING_CHARACTERS:
            return listing
        return (
            f"{listing[:_MAX_LISTING_CHARACTERS]}\n"
            f"... [truncated at {_MAX_LISTING_CHARACTERS} characters]"
        )

    def _read_file(self, arguments: Mapping[str, Any]) -> str:
        """Return one file's text, from whichever tree the path names."""
        tree, relative = self._located(_text_argument(arguments, "path"))
        return tree.read_text(relative)

    def _write_file(self, arguments: Mapping[str, Any]) -> str:
        """Write one whole file and report what was written."""
        path = _text_argument(arguments, "path")
        tree, relative = self._located(path, writing=True)
        written = tree.write_text(
            relative, _text_argument(arguments, "content", allow_empty=True)
        )
        return f"Wrote {written} bytes to {path}."

    def _delete_path(self, arguments: Mapping[str, Any]) -> str:
        """Remove one path and report that it is gone."""
        path = _text_argument(arguments, "path")
        tree, relative = self._located(path, writing=True)
        tree.delete(relative)
        return f"Removed {path}."

    def _located(self, path: str, *, writing: bool = False) -> tuple[Workspace, str]:
        """Return the tree one path names and the path relative to it.

        Raises:
            WorkspaceError: If the path names a tree this run was not given,
                or asks to write into the read-only materials.

        """
        stripped = path.strip()
        if stripped.startswith(MATERIALS_PREFIX):
            if writing:
                raise WorkspaceError(
                    f"{stripped!r} is under materials/, which is read-only: the "
                    "task's input files cannot be changed."
                )
            if self._materials is None:
                raise WorkspaceError(
                    f"{stripped!r} names materials/, but this run was given no "
                    "input files."
                )
            return self._materials, stripped[len(MATERIALS_PREFIX) :]
        if stripped.startswith(OUTPUT_PREFIX):
            if self._output is None:
                raise WorkspaceError(
                    f"{stripped!r} names output/, but this run has nowhere to "
                    "leave deliverables."
                )
            return self._output, stripped[len(OUTPUT_PREFIX) :]
        return self._workspace, stripped

    def _fetch_url(self, arguments: Mapping[str, Any]) -> str:
        """Retrieve bounded remote web evidence without executing it."""
        maximum = _bounded_integer(
            arguments, "max_characters", default=12_000, minimum=1, maximum=20_000
        )
        timeout = _bounded_integer(
            arguments, "timeout_seconds", default=20, minimum=1, maximum=45
        )
        try:
            return fetch_url(
                _text_argument(arguments, "url"), max_characters=maximum,
                timeout_seconds=timeout,
            )
        except WebError as unusable:
            raise ToolFailureError(str(unusable)) from unusable

    def _inspect_spreadsheet(self, arguments: Mapping[str, Any]) -> str:
        """Inspect a bounded spreadsheet preview from any readable tree."""
        tree, relative = self._located(_text_argument(arguments, "path"))
        sheet = arguments.get("sheet")
        if sheet is not None and (not isinstance(sheet, str) or not sheet.strip()):
            raise ToolFailureError("'sheet' must be a non-blank string when supplied.")
        max_rows = _bounded_integer(arguments, "max_rows", default=100, minimum=1, maximum=500)
        max_columns = _bounded_integer(arguments, "max_columns", default=30, minimum=1, maximum=100)
        try:
            return inspect_spreadsheet(tree.resolve(relative), sheet=sheet, max_rows=max_rows, max_columns=max_columns)
        except SpreadsheetError as unusable:
            raise ToolFailureError(str(unusable)) from unusable

    def _create_spreadsheet(self, arguments: Mapping[str, Any]) -> str:
        """Create an editable XLSX workbook at a writable contained path."""
        requested = _text_argument(arguments, "path")
        destination_tree, relative = self._located(requested, writing=True)
        try:
            return create_spreadsheet(
                destination_tree.resolve(relative), arguments.get("sheets"), title=arguments.get("title")
            )
        except SpreadsheetCreationError as unusable:
            raise ToolFailureError(str(unusable)) from unusable

    def _inspect_archive(self, arguments: Mapping[str, Any]) -> str:
        """Return a bounded inventory and optional in-place text preview of an archive."""
        tree, relative = self._located(_text_argument(arguments, "path"))
        member = arguments.get("member")
        if member is not None and (not isinstance(member, str) or not member):
            raise ToolFailureError("'member' must be a non-blank string when supplied.")
        max_entries = _bounded_integer(arguments, "max_entries", default=100, minimum=1, maximum=500)
        maximum = _bounded_integer(arguments, "max_characters", default=8_000, minimum=1, maximum=12_000)
        try:
            return inspect_archive(
                tree.resolve(relative), member=member, max_entries=max_entries,
                max_characters=maximum,
            )
        except ArchiveError as refused:
            raise ToolFailureError(str(refused)) from refused

    def _create_image(self, arguments: Mapping[str, Any]) -> str:
        """Create a bounded raster visual, resolving composition inputs safely."""
        path = _text_argument(arguments, "path")
        tree, relative = self._located(path, writing=True)
        width = arguments.get("width")
        height = arguments.get("height")
        background = arguments.get("background", "white")
        operations = arguments.get("operations", [])
        def resolve(source: str) -> Path:
            source_tree, source_relative = self._located(source)
            return source_tree.resolve(source_relative)
        try:
            return create_image(tree.resolve(relative), width=width, height=height,
                                background=background, operations=operations,
                                image_resolver=resolve)
        except ImageError as unusable:
            raise ToolFailureError(str(unusable)) from unusable

    def _create_chart(self, arguments: Mapping[str, Any]) -> str:
        """Create a bounded quantitative chart at a contained destination."""
        tree, relative = self._located(_text_argument(arguments, "path"), writing=True)
        try:
            return create_chart(
                tree.resolve(relative), chart_type=arguments.get("chart_type"),
                labels=arguments.get("labels"), values=arguments.get("values"),
                width=arguments.get("width", 1200), height=arguments.get("height", 800),
                title=arguments.get("title"), colors=arguments.get("colors"),
            )
        except ChartError as refused:
            raise ToolFailureError(str(refused)) from refused

    def _inspect_image(self, arguments: Mapping[str, Any]) -> str:
        """Return bounded metadata and optional OCR from a local image."""
        tree, relative = self._located(_text_argument(arguments, "path"))
        ocr = arguments.get("ocr", True)
        if not isinstance(ocr, bool):
            raise ToolFailureError("'ocr' must be true or false when supplied.")
        maximum = _bounded_integer(
            arguments, "max_characters", default=8_000, minimum=1, maximum=20_000
        )
        try:
            return inspect_image(tree.resolve(relative), ocr=ocr, max_characters=maximum)
        except ImageError as refused:
            raise ToolFailureError(str(refused)) from refused

    def _inspect_office(self, arguments: Mapping[str, Any]) -> str:
        """Return bounded visible text from an OOXML Word or PowerPoint file."""
        tree, relative = self._located(_text_argument(arguments, "path"))
        max_slides = _bounded_integer(arguments, "max_slides", default=10, minimum=1, maximum=100)
        max_characters = _bounded_integer(
            arguments, "max_characters", default=8_000, minimum=1, maximum=20_000
        )
        try:
            return inspect_office(tree.resolve(relative), max_slides=max_slides, max_characters=max_characters)
        except OfficeError as unusable:
            raise ToolFailureError(str(unusable)) from unusable

    def _inspect_pdf(self, arguments: Mapping[str, Any]) -> str:
        """Inspect non-active PDF evidence from a readable contained tree."""
        tree, relative = self._located(_text_argument(arguments, "path"))
        page = arguments.get("page")
        if page is not None and (isinstance(page, bool) or not isinstance(page, int) or page < 1):
            raise ToolFailureError("'page' must be a one-based whole number when supplied.")
        max_pages = _bounded_integer(arguments, "max_pages", default=5, minimum=1, maximum=25)
        max_characters = _bounded_integer(
            arguments, "max_characters", default=8_000, minimum=1, maximum=12_000
        )
        try:
            return inspect_pdf(
                tree.resolve(relative), page=page, max_pages=max_pages,
                max_characters=max_characters,
            )
        except PdfError as unusable:
            raise ToolFailureError(str(unusable)) from unusable

    def _create_pdf(self, arguments: Mapping[str, Any]) -> str:
        """Create a static PDF at a writable contained path."""
        requested = _text_argument(arguments, "path")
        destination_tree, relative = self._located(requested, writing=True)

        def locate_image(path: str) -> Path:
            tree, image_relative = self._located(path)
            return tree.resolve(image_relative)

        try:
            return create_pdf(
                destination_tree.resolve(relative), arguments.get("blocks"),
                image_path=locate_image, title=arguments.get("title"),
            )
        except PdfCreationError as unusable:
            raise ToolFailureError(str(unusable)) from unusable

    def _inspect_email(self, arguments: Mapping[str, Any]) -> str:
        """Inspect an email evidence file from a readable contained tree."""
        tree, relative = self._located(_text_argument(arguments, "path"))
        message_index = _bounded_integer(arguments, "message_index", default=0, minimum=0, maximum=10_000)
        max_messages = _bounded_integer(arguments, "max_messages", default=20, minimum=1, maximum=50)
        try:
            return inspect_email(
                tree.resolve(relative),
                message_index=message_index,
                max_messages=max_messages,
            )
        except EmailError as unusable:
            raise ToolFailureError(str(unusable)) from unusable

    def _create_document(self, arguments: Mapping[str, Any]) -> str:
        """Create an editable Word document at a writable contained path."""
        requested = _text_argument(arguments, "path")
        destination_tree, relative = self._located(requested, writing=True)

        def locate_image(path: str) -> Path:
            tree, image_relative = self._located(path)
            return tree.resolve(image_relative)

        try:
            return create_document(
                destination_tree.resolve(relative), arguments.get("blocks"),
                image_path=locate_image, title=arguments.get("title"),
            )
        except DocumentError as unusable:
            raise ToolFailureError(str(unusable)) from unusable

    def _create_presentation(self, arguments: Mapping[str, Any]) -> str:
        """Create an editable slide deck at a writable contained path."""
        requested = _text_argument(arguments, "path")
        destination_tree, relative = self._located(requested, writing=True)

        def locate_image(path: str) -> Path:
            tree, image_relative = self._located(path)
            return tree.resolve(image_relative)

        try:
            result = create_presentation(
                destination_tree.resolve(relative), arguments.get("slides"), image_path=locate_image
            )
        except PresentationError as unusable:
            raise ToolFailureError(str(unusable)) from unusable
        return result

    def _run_command(self, arguments: Mapping[str, Any]) -> str:
        """Run one command and report how it ended and what it printed.

        Raises:
            ToolFailureError: If the command is not a list of strings, or the
                timeout is not a whole number.

        """
        command = _command_argument(arguments)
        try:
            result = self._commands.run(
                command, timeout_seconds=_timeout_argument(arguments)
            )
        except (TypeError, ValueError) as unusable:
            raise ToolFailureError(str(unusable)) from unusable
        status = (
            "timed out"
            if result.timed_out
            else f"exit code {result.exit_code}"
            if result.exit_code is not None
            else "did not start"
        )
        return "\n".join(
            (
                f"$ {' '.join(result.command)}",
                f"[{status}]",
                f"--- stdout ---\n{result.stdout}",
                f"--- stderr ---\n{result.stderr}",
            )
        )


def _text_argument(
    arguments: Mapping[str, Any], name: str, *, allow_empty: bool = False
) -> str:
    """Return one string argument.

    Raises:
        ToolFailureError: If the argument is absent, not a string, or blank when a
            value is required.

    """
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ToolFailureError(f"{name!r} is required and must be a string.")
    if not allow_empty and not value.strip():
        raise ToolFailureError(f"{name!r} must not be blank.")
    return value


def _command_argument(arguments: Mapping[str, Any]) -> Sequence[str]:
    """Return the argument vector a command call named.

    Raises:
        ToolFailureError: If it is not a non-empty list of strings.

    """
    value = arguments.get("command")
    if isinstance(value, str) or not isinstance(value, list):
        raise ToolFailureError(
            "'command' must be a list of strings, e.g. ['python', '--version']."
        )
    if not value:
        raise ToolFailureError("'command' needs at least the program to run.")
    if not all(isinstance(argument, str) for argument in value):
        raise ToolFailureError("Every element of 'command' must be a string.")
    return [str(argument) for argument in value]


def _timeout_argument(arguments: Mapping[str, Any]) -> int | None:
    """Return the bound a command call asked for, if it asked for one.

    Raises:
        ToolFailureError: If the bound is not a positive whole number.

    """
    value = arguments.get("timeout_seconds")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolFailureError("'timeout_seconds' must be a whole number of seconds.")
    if value <= 0:
        raise ToolFailureError("'timeout_seconds' must be greater than zero.")
    return int(value)


def _bounded_integer(
    arguments: Mapping[str, Any], name: str, *, default: int, minimum: int, maximum: int
) -> int:
    """Return an optional integer constrained to an explicit safe range."""
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ToolFailureError(f"{name!r} must be a whole number from {minimum} to {maximum}.")
    return value
