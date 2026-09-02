"""Strict, bounded inspection of single-document YAML data.

YAML is useful for task configuration, but its graph features are unsuitable for
an inspection result: aliases can amplify data, explicit tags can request
application-defined construction, and duplicate mapping keys have ambiguous
meaning.  This reader accepts only an ordinary single YAML document with no
anchors, aliases, explicit tags, duplicate keys, or excessively deep/large
node tree.  It parses scalars using PyYAML's safe YAML 1.1 scalar semantics and
renders a JSON-shaped bounded preview.

Contract: ``pointer`` is an RFC 6901 pointer to any value (empty selects the
root); object keys are strings, and list segments are decimal indexes.  Missing
or malformed pointers are rejected rather than silently selecting a neighbour.
The file-size, depth, node-count, and rendered-text limits are inclusive: a
value exactly at a limit is accepted, and the next byte/node/level is refused.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from yaml.events import AliasEvent, DocumentEndEvent, DocumentStartEvent, MappingStartEvent, NodeEvent, ScalarEvent, SequenceStartEvent
from yaml.tokens import AliasToken, AnchorToken, TagToken

MAX_YAML_BYTES = 32 * 1024 * 1024
MAX_YAML_NODES = 200_000
MAX_YAML_DEPTH = 100
MAX_YAML_TEXT_CHARACTERS = 120_000


class YamlDataError(Exception):
    """A YAML file could not be inspected under the strict data contract."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe loader which makes duplicate YAML mapping keys an error."""


def _construct_unique_mapping(loader: _UniqueKeySafeLoader, node: yaml.nodes.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise YamlDataError("YAML mapping keys must be scalar, hashable values.") from error
        if duplicate:
            raise YamlDataError(f"Duplicate YAML mapping key {key!r} is not allowed.")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def inspect_yaml(path: Path, *, pointer: str = "") -> str:
    """Return a bounded JSON rendering of strict YAML data selected by pointer."""
    if not path.is_file():
        raise YamlDataError(f"{path.name!r} is not a YAML file.")
    file_bytes = path.stat().st_size
    if file_bytes > MAX_YAML_BYTES:
        raise YamlDataError(f"The file exceeds the {MAX_YAML_BYTES}-byte YAML limit.")
    if not isinstance(pointer, str):
        raise YamlDataError("pointer must be a string JSON Pointer.")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise YamlDataError(f"Could not read YAML data: {error}") from error
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise YamlDataError(f"YAML must be UTF-8 text: {error}") from error
    _validate_events_and_tokens(text)
    try:
        value = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except YamlDataError:
        raise
    except yaml.YAMLError as error:
        raise YamlDataError(f"Could not parse YAML data: {error}") from error
    _validate_json_shaped(value)
    selected = _select(value, pointer)
    result = {
        "format": "YAML",
        "selection": pointer or "root",
        "file_bytes": file_bytes,
        "inferred_type": _type_name(selected),
        "value": selected,
    }
    try:
        rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise YamlDataError(f"YAML value cannot be rendered as JSON data: {error}") from error
    if len(rendered) > MAX_YAML_TEXT_CHARACTERS:
        return rendered[:MAX_YAML_TEXT_CHARACTERS] + f"\n... [truncated at {MAX_YAML_TEXT_CHARACTERS} characters]"
    return rendered


def _validate_events_and_tokens(text: str) -> None:
    """Reject graph/type features and establish document and complexity bounds."""
    try:
        for token in yaml.scan(text):
            if isinstance(token, AnchorToken):
                raise YamlDataError("YAML anchors are not supported.")
            if isinstance(token, AliasToken):
                raise YamlDataError("YAML aliases are not supported.")
            if isinstance(token, TagToken):
                raise YamlDataError("Explicit YAML tags are not supported.")
        documents = 0
        nodes = 0
        depth = 0
        for event in yaml.parse(text):
            if isinstance(event, DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise YamlDataError("YAML must contain exactly one document.")
            if isinstance(event, AliasEvent):
                raise YamlDataError("YAML aliases are not supported.")
            if isinstance(event, NodeEvent):
                nodes += 1
                if nodes > MAX_YAML_NODES:
                    raise YamlDataError(f"YAML exceeds the {MAX_YAML_NODES}-node limit.")
            if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    raise YamlDataError(f"YAML exceeds the {MAX_YAML_DEPTH}-level nesting limit.")
            # YAML event streams close every collection with a matching end
            # event. Importing their two classes solely for this check makes
            # the test easy to read and avoids constructing a graph first.
            if event.__class__.__name__ in {"MappingEndEvent", "SequenceEndEvent"}:
                depth -= 1
        if documents != 1:
            raise YamlDataError("YAML must contain exactly one non-empty document.")
    except YamlDataError:
        raise
    except yaml.YAMLError as error:
        raise YamlDataError(f"Could not parse YAML data: {error}") from error


def _validate_json_shaped(value: Any) -> None:
    """Require string mapping keys and JSON-compatible scalar values."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise YamlDataError("YAML mapping keys must be strings for JSON Pointer selection.")
                pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)
        elif current is None or isinstance(current, (str, int, float, bool)):
            continue
        else:
            raise YamlDataError(f"Unsupported YAML value type {type(current).__name__}.")


def _select(value: Any, pointer: str) -> Any:
    if not pointer:
        return value
    if not pointer.startswith("/"):
        raise YamlDataError("pointer must be empty or begin with '/'.")
    current = value
    for encoded in pointer[1:].split("/"):
        segment = _unescape(encoded)
        if isinstance(current, Mapping):
            if segment not in current:
                raise YamlDataError(f"pointer does not name a value: {pointer!r}.")
            current = current[segment]
        elif isinstance(current, list):
            if not segment.isdecimal() or (len(segment) > 1 and segment.startswith("0")):
                raise YamlDataError(f"pointer list segment {segment!r} is not a valid array index.")
            index = int(segment)
            if index >= len(current):
                raise YamlDataError(f"pointer does not name a value: {pointer!r}.")
            current = current[index]
        else:
            raise YamlDataError(f"pointer cannot descend into a {_type_name(current)} value.")
    return current


def _unescape(segment: str) -> str:
    # Decode once from left to right.  Chained ``replace`` calls get ``~01``
    # wrong: it is the valid encoding of a literal ``~1`` key, not an escape
    # introduced for a second decoding pass.
    pieces: list[str] = []
    index = 0
    while index < len(segment):
        character = segment[index]
        if character != "~":
            pieces.append(character)
            index += 1
            continue
        if index + 1 == len(segment) or segment[index + 1] not in "01":
            raise YamlDataError("pointer has an invalid '~' escape.")
        pieces.append("~" if segment[index + 1] == "0" else "/")
        index += 2
    return "".join(pieces)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"
