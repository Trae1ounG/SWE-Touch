from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from harbor.swe_touch.io import read_jsonl, sha256_file

USER_SIMULATOR_PROMPT_ID = "counter_edit_user_simulator"

FORBIDDEN_PATTERNS = {
    "private filesystem path": re.compile(r"/(?:mnt/bn|data00/home|home/tiger)/"),
    "internal hostname": re.compile(
        r"(?:byted\.org|bytedance\.net|workspace\.byted\.org)"
    ),
    "private backend": re.compile(
        r"\b(?:swalm|doas-token|model_hub|super-relay)\b", re.IGNORECASE
    ),
    "credential": re.compile(
        r"(?:api[_-]?key|auth[_-]?token)[\"'\s:=]+[A-Za-z0-9_-]{16,}", re.IGNORECASE
    ),
}


def validate_path(path: Path) -> dict[str, Any]:
    files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    if not files:
        raise ValueError(f"no JSONL files found under {path}")
    records = 0
    identities: set[tuple[str, str]] = set()
    for file in files:
        for row in read_jsonl(file):
            validate_record(row)
            identity = (row["benchmark"], row["instance_id"])
            if identity in identities:
                raise ValueError(f"duplicate task record: {identity}")
            identities.add(identity)
            records += 1
        scan_sensitive_text(file.read_text(encoding="utf-8"), source=str(file))
    return {
        "files": {file.name: sha256_file(file) for file in files},
        "records": records,
    }


def validate_record(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "benchmark",
        "instance_id",
        "task_critical_regions",
        "counter_edit",
    }
    missing = required - row.keys()
    if missing:
        raise ValueError(f"{row.get('instance_id')}: missing fields {sorted(missing)}")
    if row["schema_version"] != "1.0.0":
        raise ValueError(f"unsupported schema version: {row['schema_version']}")
    if row["benchmark"] not in {"swe_bench_verified", "swe_bench_pro", "deepswe"}:
        raise ValueError(f"unknown benchmark: {row['benchmark']}")
    _validate_regions(row["task_critical_regions"], row["instance_id"])
    counter = row["counter_edit"]
    if counter.get("message_prompt_id") != USER_SIMULATOR_PROMPT_ID:
        raise ValueError(
            f"{row['instance_id']}: unsupported user simulator prompt "
            f"{counter.get('message_prompt_id')!r}"
        )
    interventions = counter.get("interventions") or []
    if len(interventions) != int(counter.get("max_interventions") or 0):
        raise ValueError(f"{row['instance_id']}: intervention count mismatch")
    for expected_order, intervention in enumerate(interventions, 1):
        if intervention.get("order") != expected_order:
            raise ValueError(f"{row['instance_id']}: interventions are not ordered")
        trigger = intervention.get("trigger") or {}
        if trigger.get("event") not in {"read", "edit", "read_or_edit"}:
            raise ValueError(f"{row['instance_id']}: invalid trigger event")
        _validate_regions(trigger.get("regions") or [], row["instance_id"])
        patch = intervention.get("patch")
        if patch:
            diff = patch.get("diff") or ""
            if not diff.startswith("diff --git "):
                raise ValueError(
                    f"{row['instance_id']}: patch is not a unified git diff"
                )
            scan_sensitive_text(
                json.dumps(patch, ensure_ascii=False), source=row["instance_id"]
            )


def _validate_regions(regions: Iterable[dict[str, Any]], instance_id: str) -> None:
    for region in regions:
        path = region.get("path")
        start = region.get("start_line")
        end = region.get("end_line")
        if not isinstance(path, str) or not path or path.startswith("/"):
            raise ValueError(f"{instance_id}: invalid region path {path!r}")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start < 1
            or end < start
        ):
            raise ValueError(f"{instance_id}: invalid region {path}:{start}-{end}")


def scan_sensitive_text(text: str, *, source: str) -> None:
    for label, pattern in FORBIDDEN_PATTERNS.items():
        match = pattern.search(text)
        if match:
            raise ValueError(f"{source}: found {label}: {match.group(0)!r}")
