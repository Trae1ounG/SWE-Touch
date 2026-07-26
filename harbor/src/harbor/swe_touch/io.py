from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TypeError(f"{path}:{line_number}: expected a JSON object")
        rows.append(value)
    return rows


def read_records(path: Path) -> list[dict[str, Any]]:
    """Read task records without changing their schema.

    A pipeline may emit one JSON record while a release contains many records in JSONL.
    Both containers carry the exact same record object consumed by evaluation.
    """

    if path.is_dir():
        rows: list[dict[str, Any]] = []
        for child in sorted(path.glob("*.jsonl")):
            rows.extend(read_jsonl(child))
        return rows
    if path.suffix == ".jsonl":
        return read_jsonl(path)
    value = read_json(path)
    if not isinstance(value, dict):
        raise TypeError(f"{path}: expected a JSON record")
    return [value]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def write_record(path: Path, row: dict[str, Any]) -> None:
    """Write one task record using JSON or one-row JSONL container syntax."""

    if path.suffix == ".jsonl":
        write_jsonl(path, [row])
    else:
        write_json(path, row)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
