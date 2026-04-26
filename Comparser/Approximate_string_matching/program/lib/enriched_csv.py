from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Sequence

from .constants import DIAGNOSTIC_HEADER, ENRICHED_DIAGNOSTIC_HEADER, ENRICHED_HEADER, PIPE_COMPAT_HEADER
from .csv_utils import read_delimited_dicts, write_dict_rows


def read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        try:
            header = next(reader)
        except StopIteration as exc:
            raise RuntimeError(f"{path} is empty.") from exc
        return list(header), [list(row) for row in reader]


def read_dict_rows(path: Path) -> list[dict[str, str]]:
    return read_delimited_dicts(path)


def read_diagnostic_speech_rows(path: Path) -> list[dict[str, str]]:
    rows = read_dict_rows(path)
    return [row for row in rows if (row.get("Kind") or "").strip().lower() == "speech"]


def diagnostic_row_to_base(row: dict[str, str]) -> dict[str, str]:
    return {column: (row.get(column) or "") for column in PIPE_COMPAT_HEADER}


def diagnostic_metadata(row: dict[str, str]) -> dict[str, str]:
    return {column: (row.get(column) or "") for column in ENRICHED_DIAGNOSTIC_HEADER}


def rows_from_dicts(dict_rows: Sequence[dict[str, str]], header: Sequence[str]) -> list[list[str]]:
    return [[row.get(column, "") for column in header] for row in dict_rows]


def dicts_from_rows(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for row in rows:
        data = {column: (row[index] if index < len(row) else "") for index, column in enumerate(header)}
        output.append(data)
    return output


def ensure_row_width(rows: list[list[str]], width: int) -> None:
    for row in rows:
        if len(row) < width:
            row.extend([""] * (width - len(row)))


def merge_pipe_values(*values: str) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for fragment in value.split("|"):
            cleaned = fragment.strip()
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(cleaned)
    return " | ".join(merged)


def first_kept_row_index(dict_rows: Sequence[dict[str, str]]) -> int | None:
    for index, row in enumerate(dict_rows):
        if (row.get("Keep") or "").strip().lower() == "x":
            return index
    return 0 if dict_rows else None


def build_enriched_dict_rows(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    source_rows: Sequence[dict[str, str]],
    extra_columns: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    legacy_rows = dicts_from_rows(header, rows)
    output: list[dict[str, str]] = []
    for index, legacy_row in enumerate(legacy_rows):
        source_row = source_rows[index] if index < len(source_rows) else {}
        data = {column: legacy_row.get(column, "") for column in ENRICHED_HEADER}
        for column in ENRICHED_DIAGNOSTIC_HEADER:
            data[column] = source_row.get(column, "")
        if extra_columns:
            for column in extra_columns:
                data[column] = legacy_row.get(column, "")
        output.append(data)
    return output


def write_enriched_rows(path: Path, rows: Iterable[dict[str, str]]) -> None:
    write_dict_rows(path, rows, ENRICHED_HEADER)


def validate_stage07_diagnostic_header(header: Sequence[str]) -> None:
    required = set(DIAGNOSTIC_HEADER)
    missing = [column for column in DIAGNOSTIC_HEADER if column not in header]
    if missing:
        raise RuntimeError(f"Stage-07 diagnostic CSV is missing required columns: {', '.join(missing)}")
