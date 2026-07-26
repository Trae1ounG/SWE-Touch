from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any


def normalize_region(path: str, start_line: Any, end_line: Any) -> dict[str, Any]:
    start = int(start_line)
    end = int(end_line)
    if not path or path.startswith("/"):
        raise ValueError(f"region path must be repository-relative: {path!r}")
    if start < 1 or end < start:
        raise ValueError(f"invalid region {path}:{start}-{end}")
    return {"path": path, "start_line": start, "end_line": end}


def regions_from_mapping(value: dict[str, Any] | None) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    for path in sorted(value or {}):
        for span in (value or {}).get(path) or []:
            if not isinstance(span, list) or len(span) != 2:
                raise ValueError(f"invalid span for {path}: {span!r}")
            regions.append(normalize_region(path, span[0], span[1]))
    return regions


def normalize_regions(value: Any) -> list[dict[str, Any]]:
    """Normalize the public region list or the legacy path-to-spans mapping."""

    if value is None:
        return []
    if isinstance(value, dict):
        return regions_from_mapping(value)
    if not isinstance(value, list):
        raise TypeError("regions must be a list or path-to-spans mapping")
    regions = []
    for row in value:
        if not isinstance(row, dict):
            raise TypeError(f"region must be an object: {row!r}")
        region = normalize_region(
            str(row.get("path") or ""),
            row.get("start_line"),
            row.get("end_line"),
        )
        if isinstance(row.get("evidence"), dict):
            region["evidence"] = row["evidence"]
        regions.append(region)
    return regions


def merge_line_spans(lines: Iterable[int]) -> list[tuple[int, int]]:
    ordered = sorted({int(line) for line in lines if int(line) > 0})
    if not ordered:
        return []
    spans: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for line in ordered[1:]:
        if line == previous + 1:
            previous = line
            continue
        spans.append((start, previous))
        start = previous = line
    spans.append((start, previous))
    return spans


def mine_critical_regions(
    trajectories: Iterable[dict[str, Any]],
    *,
    minimum_models: int = 2,
    max_regions: int = 8,
    reference_patch: str | None = None,
) -> list[dict[str, Any]]:
    """Mine contiguous edit regions supported by multiple model trajectories.

    Input trajectories use the public interchange form::

        {"model": "...", "edits": [{"path": "src/a.py", "lines": [10, 11]}]}

    If no edit line reaches ``minimum_models``, the threshold is reduced to one model. This
    mirrors the release policy: prefer stable cross-trajectory overlap, then retain available
    edit evidence instead of silently dropping the task.
    """

    support: dict[tuple[str, int], set[str]] = defaultdict(set)
    for trajectory in trajectories:
        model = str(trajectory.get("model") or "unknown")
        for edit in trajectory.get("edits") or []:
            path = str(edit.get("path") or "")
            for line in edit.get("lines") or []:
                support[(path, int(line))].add(model)

    threshold = minimum_models
    selected = {key for key, models in support.items() if len(models) >= threshold}
    if not selected:
        threshold = 1
        selected = set(support)
    evidence_kind = "trajectory_edit"
    if not selected and reference_patch:
        evidence_kind = "reference_patch_fallback"
        support.update(_reference_patch_lines(reference_patch))
        selected = set(support)

    by_path: dict[str, list[int]] = defaultdict(list)
    for path, line in selected:
        by_path[path].append(line)

    regions: list[dict[str, Any]] = []
    for path in sorted(by_path):
        for start, end in merge_line_spans(by_path[path]):
            evidence_models = sorted(
                set().union(*(support[(path, line)] for line in range(start, end + 1)))
            )
            region = normalize_region(path, start, end)
            region["evidence"] = {
                "kind": evidence_kind,
                "models": evidence_models,
                "minimum_model_support": threshold,
            }
            regions.append(region)

    regions.sort(
        key=lambda row: (
            -len((row.get("evidence") or {}).get("models") or []),
            row["path"],
            row["start_line"],
        )
    )
    return regions[:max_regions]


def _reference_patch_lines(diff: str) -> dict[tuple[str, int], set[str]]:
    import re

    result: dict[tuple[str, int], set[str]] = defaultdict(set)
    path = ""
    new_line = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            continue
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if match:
            new_line = int(match.group(1))
            continue
        if not path or not new_line:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            result[(path, new_line)].add("reference_patch")
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_line += 1
    return result
