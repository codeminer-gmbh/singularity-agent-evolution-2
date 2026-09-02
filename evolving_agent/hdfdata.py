"""Bounded inspection of HDF5 and HDF5-backed NetCDF4 scientific containers."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import h5py
import numpy as np

MAX_HDF5_FILE_BYTES = 512 * 1024 * 1024
MAX_HDF5_ELEMENTS = 1_000
MAX_HDF5_OFFSET = 1_000_000
MAX_HDF5_DATASETS = 500
MAX_HDF5_TEXT_CHARACTERS = 120_000
_MAX_ATTRIBUTES = 50
_MAX_VALUE_CHARACTERS = 2_000


class HDF5Error(Exception):
    """An HDF5 file or requested dataset could not be inspected."""


def inspect_hdf5(path: Path, *, dataset: str | None = None, max_elements: int = 100, offset: int = 0) -> str:
    """Describe datasets and preview a bounded sequence of logical array elements."""
    if not path.is_file():
        raise HDF5Error(f"{path.name!r} is not an HDF5/NetCDF4 file.")
    if path.stat().st_size > MAX_HDF5_FILE_BYTES:
        raise HDF5Error(f"The file exceeds the {MAX_HDF5_FILE_BYTES}-byte HDF5 limit.")
    if isinstance(max_elements, bool) or not isinstance(max_elements, int) or not 1 <= max_elements <= MAX_HDF5_ELEMENTS:
        raise HDF5Error(f"max_elements must be a whole number from 1 to {MAX_HDF5_ELEMENTS}.")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_HDF5_OFFSET:
        raise HDF5Error(f"offset must be a whole number from 0 to {MAX_HDF5_OFFSET}.")
    if dataset is not None and (not isinstance(dataset, str) or not dataset):
        raise HDF5Error("dataset must be a non-empty dataset path.")
    try:
        with h5py.File(path, "r") as source:
            datasets = _datasets(source)
            lines = [
                f"HDF5 file: {path.name}",
                f"Root attributes: {_attribute_names(source)}", 
                f"Datasets: {len(datasets)}" + (f" (showing first {MAX_HDF5_DATASETS})" if len(datasets) >= MAX_HDF5_DATASETS else "."),
            ]
            lines.extend(_describe(item) for item in datasets)
            if dataset is not None:
                if dataset not in source or not isinstance(source[dataset], h5py.Dataset):
                    raise HDF5Error(f"Unknown HDF5 dataset {dataset!r}.")
                selected = source[dataset]
                lines.append("")
                lines.append(f"Preview of {selected.name}: {_preview(selected, max_elements, offset)}")
    except HDF5Error:
        raise
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise HDF5Error(f"Could not read HDF5 data: {error}") from error
    return _truncate("\n".join(lines))


def _datasets(source: h5py.File) -> list[h5py.Dataset]:
    found: list[h5py.Dataset] = []

    def visitor(_: str, item: h5py.Group | h5py.Dataset) -> None:
        if isinstance(item, h5py.Dataset) and len(found) < MAX_HDF5_DATASETS:
            found.append(item)

    source.visititems(visitor)
    return found


def _describe(item: h5py.Dataset) -> str:
    shape = "scalar" if item.shape == () else str(tuple(item.shape))
    chunks = "contiguous" if item.chunks is None else str(tuple(item.chunks))
    return f"- {item.name}: shape={shape}, dtype={item.dtype}, chunks={chunks}, attributes={_attribute_names(item)}"


def _attribute_names(item: h5py.File | h5py.Dataset) -> str:
    names = [str(name) for name in list(item.attrs.keys())[:_MAX_ATTRIBUTES]]
    suffix = ", ..." if len(item.attrs) > _MAX_ATTRIBUTES else ""
    return "[" + ", ".join(names) + suffix + "]"


def _preview(item: h5py.Dataset, maximum: int, offset: int) -> str:
    total = int(math.prod(item.shape)) if item.shape else 1
    if offset >= total:
        return f"0 values (offset {offset}; dataset has {total} values)."
    count = min(maximum, total - offset)
    values: list[Any] = []
    for flat_index in range(offset, offset + count):
        if item.shape:
            coordinate = tuple(np.unravel_index(flat_index, item.shape))
            value = item[coordinate]
        else:
            value = item[()]
        values.append(_value(value))
    body = json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"{count} values" + (f" after flat offset {offset}" if offset else "") + f": {body}"


def _value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return [_value(part) for part in value.tolist()]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")[:_MAX_VALUE_CHARACTERS]
    if isinstance(value, str):
        return value[:_MAX_VALUE_CHARACTERS]
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _truncate(text: str) -> str:
    if len(text) <= MAX_HDF5_TEXT_CHARACTERS:
        return text
    return text[:MAX_HDF5_TEXT_CHARACTERS] + f"\n... [truncated at {MAX_HDF5_TEXT_CHARACTERS} characters]"
