from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

from harbor.swe_touch.runtime.command_events import events_from_shell_command

Interval = tuple[int | None, int | None]
RegionMap = dict[str, list[Interval]]

_CODE_EXTENSIONS = (
    "py|pyx|pxd|txt|rst|md|yml|yaml|toml|cfg|ini|sql|html|css|scss|"
    "js|jsx|ts|tsx|go|rs|java|kt|kts|c|cc|cpp|cxx|h|hpp|hh|cs|rb|php|"
    "swift|scala|sh|bash|zsh|fish|lua|r|ex|exs|erl|hrl|clj|cljs"
)
_PATH_RE = re.compile(
    rf"(?P<path>(?:/testbed/|\./)?[A-Za-z0-9_./-]+\.({_CODE_EXTENSIONS}))"
)
_ALLOWED_PATH_RE = re.compile(rf"^[A-Za-z0-9_./-]+\.({_CODE_EXTENSIONS})$")
_PIPE_SED_RE = re.compile(
    r"\b(?:cat|nl)(?:\s+-[^\s]+)*\s+(?P<path>[^\s|;]+).*?"
    r"\|\s*sed\s+-n\s+['\"]?(?P<start>\d+),(?P<end>\d+)p['\"]?"
)
_GREP_PATH_LINE_RE = re.compile(
    rf"(?P<path>/testbed/[^:\s]+|\./[^:\s]+|[A-Za-z0-9_./-]+\.({_CODE_EXTENSIONS})):(?P<line>\d+):"
)
_GREP_LINE_RE = re.compile(r"^(?P<line>\d+):")
_NUMBERED_OUTPUT_RE = re.compile(r"^\s*(?P<line>\d+)\s+(?:\t|\S)")
_DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
_DIFF_HUNK_RE = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<length>\d+))? @@"
)


@dataclass(frozen=True)
class CommandRecord:
    label: str
    step: int
    command: str
    output: str = ""


@dataclass(frozen=True)
class RegionEvent:
    event_type: str
    path: str
    start_line: int | None = None
    end_line: int | None = None
    label: str | None = None
    step: int | None = None
    command: str | None = None


def command_records_from_mini_trajectory(
    payload: dict[str, Any], *, label: str
) -> list[CommandRecord]:
    pending: dict[str, tuple[int, str]] = {}
    records: list[CommandRecord] = []
    for idx, message in enumerate(payload.get("messages") or []):
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            if function.get("name") != "bash":
                continue
            command = _command_from_arguments(function.get("arguments"))
            if command is None:
                continue
            call_id = str(tool_call.get("id") or f"{idx}:{len(pending)}")
            pending[call_id] = (idx, command)
        if message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        if call_id not in pending:
            continue
        step, command = pending.pop(call_id)
        records.append(
            CommandRecord(
                label=label, step=step, command=command, output=_tool_output(message)
            )
        )
    for step, command in pending.values():
        records.append(CommandRecord(label=label, step=step, command=command))
    return records


def events_from_command_record(record: CommandRecord) -> list[RegionEvent]:
    events: list[RegionEvent] = []
    for event in events_from_shell_command(record.command):
        if event.event_type != "read":
            continue
        path = _normalize_path(event.path)
        if not path:
            continue
        events.append(
            RegionEvent(
                event_type="read",
                path=path,
                start_line=event.start_line,
                end_line=event.end_line,
                label=record.label,
                step=record.step,
                command=record.command,
            )
        )
    events.extend(_pipe_sed_events(record))
    events.extend(_grep_output_events(record))
    events.extend(_numbered_output_events(record))
    return _dedupe_events(events)


def build_critical_region_artifact(
    *,
    instance_id: str,
    trajectories: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    model_order = list(trajectories)
    reads_by_model: dict[str, RegionMap] = {}
    read_step_info: dict[str, list[dict[str, Any]]] = {}
    modified_by_model: dict[str, list[str]] = {}
    modified_regions_by_model: dict[str, RegionMap] = {}

    for label, payload in trajectories.items():
        events = [
            event
            for record in command_records_from_mini_trajectory(payload, label=label)
            for event in events_from_command_record(record)
        ]
        reads_by_model[label] = _merge_region_map(_events_to_region_map(events))
        read_step_info[label] = [
            {
                "path": event.path,
                "start_line": event.start_line,
                "end_line": event.end_line,
                "step": event.step,
                "command": event.command,
            }
            for event in events
        ]
        modified_by_model[label] = modified_files_from_trajectory(payload, label=label)
        modified_regions_by_model[label] = modified_regions_from_trajectory(
            payload, label=label
        )

    read_core_regions = _intersect_region_maps(list(reads_by_model.values()))
    read_optional_regions_map = {
        label: _subtract_region_maps(regions, read_core_regions)
        for label, regions in reads_by_model.items()
    }
    modified_core_files = _intersect_lists(list(modified_by_model.values()))
    modified_region_evidence_models = [
        label for label, regions in modified_regions_by_model.items() if regions
    ]
    modified_core_regions = _intersect_region_maps(
        [regions for regions in modified_regions_by_model.values() if regions]
    )
    main_files = sorted(set(read_core_regions) & set(modified_core_files))

    return {
        "instance_id": instance_id,
        "models": model_order,
        "read_core_files": sorted(read_core_regions),
        "read_core_regions": _json_region_map(read_core_regions),
        "read_optional_files_map": {
            label: sorted(regions)
            for label, regions in read_optional_regions_map.items()
        },
        "read_optional_regions_map": {
            label: _json_region_map(regions)
            for label, regions in read_optional_regions_map.items()
        },
        "modified_files_by_model": modified_by_model,
        "modified_regions_by_model": {
            label: _json_region_map(regions)
            for label, regions in modified_regions_by_model.items()
        },
        "modified_region_evidence_models": modified_region_evidence_models,
        "modified_core_files": modified_core_files,
        "modified_core_regions": _json_region_map(modified_core_regions),
        "main_files": main_files,
        "read_step_info": read_step_info,
    }


def modified_regions_from_trajectory(
    payload: dict[str, Any], *, label: str
) -> RegionMap:
    regions = modified_regions_from_diff(
        str((payload.get("info") or {}).get("submission") or "")
    )
    records = command_records_from_mini_trajectory(payload, label=label)
    for record in records:
        if "diff --git " in record.output:
            regions = _merge_region_map(
                _union_region_maps(regions, modified_regions_from_diff(record.output))
            )
    return regions


def modified_regions_from_diff(diff_text: str) -> RegionMap:
    regions: RegionMap = {}
    current_path: str | None = None
    for line in diff_text.splitlines():
        file_match = _DIFF_FILE_RE.match(line)
        if file_match:
            current_path = _normalize_path(file_match.group(2))
            continue
        hunk_match = _DIFF_HUNK_RE.match(line)
        if hunk_match and current_path:
            start = int(hunk_match.group("start"))
            length = int(hunk_match.group("length") or "1")
            if length <= 0:
                continue
            regions.setdefault(current_path, []).append((start, start + length - 1))
    return _merge_region_map(regions)


def modified_files_from_trajectory(payload: dict[str, Any], *, label: str) -> list[str]:
    files = set(
        modified_files_from_submission(
            str((payload.get("info") or {}).get("submission") or "")
        )
    )
    records = command_records_from_mini_trajectory(payload, label=label)
    for record in records:
        if "diff --git " in record.output:
            files.update(modified_files_from_submission(record.output))
    if files:
        return sorted(files)
    for record in records:
        files.update(_write_paths_from_command(record.command))
    return sorted(files)


def modified_files_from_submission(submission: str) -> list[str]:
    files: set[str] = set()
    for match in _DIFF_FILE_RE.finditer(submission):
        path = _normalize_path(match.group(2))
        if path:
            files.add(path)
    if files:
        return sorted(files)
    for line in submission.splitlines():
        if not line.startswith("+++ b/"):
            continue
        path = _normalize_path(line[len("+++ b/") :])
        if path:
            files.add(path)
    return sorted(files)


def _write_paths_from_command(command: str) -> list[str]:
    files: set[str] = set()
    for pattern in [
        re.compile(r">\s*(?P<path>/testbed/[^\s]+|[A-Za-z0-9_./-]+\.[A-Za-z0-9]+)"),
        re.compile(
            r"open\([\'\"](?P<path>[^\'\"]+)[\'\"]\s*,\s*[\'\"][wa][^\'\"]*[\'\"]"
        ),
        re.compile(r"Path\([\'\"](?P<path>[^\'\"]+)[\'\"]\)\.write_text"),
    ]:
        for match in pattern.finditer(command):
            path = _normalize_path(match.group("path"))
            if path:
                files.add(path)
    return sorted(files)


def _command_from_arguments(arguments: Any) -> str | None:
    if isinstance(arguments, dict):
        command = arguments.get("command")
        return command if isinstance(command, str) else None
    if not isinstance(arguments, str):
        return None
    try:
        data = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    command = data.get("command")
    return command if isinstance(command, str) else None


def _tool_output(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, str):
        return ""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content
    if not isinstance(data, dict):
        return content
    parts = [data.get(key) for key in ("output", "output_head", "output_tail")]
    return "\n".join(part for part in parts if isinstance(part, str))


def _pipe_sed_events(record: CommandRecord) -> list[RegionEvent]:
    events: list[RegionEvent] = []
    for match in _PIPE_SED_RE.finditer(record.command):
        path = _normalize_path(match.group("path"))
        if path:
            events.append(
                RegionEvent(
                    event_type="read",
                    path=path,
                    start_line=int(match.group("start")),
                    end_line=int(match.group("end")),
                    label=record.label,
                    step=record.step,
                    command=record.command,
                )
            )
    return events


def _grep_output_events(record: CommandRecord) -> list[RegionEvent]:
    events: list[RegionEvent] = []
    for line in record.output.splitlines():
        match = _GREP_PATH_LINE_RE.search(line)
        if match:
            path = _normalize_path(match.group("path"))
            if path:
                line_no = int(match.group("line"))
                events.append(_line_event(record, path, line_no))
            continue
        if not _looks_like_grep(record.command):
            continue
        line_match = _GREP_LINE_RE.match(line)
        if not line_match:
            continue
        paths = _command_paths(record.command)
        if len(paths) != 1:
            continue
        events.append(_line_event(record, paths[0], int(line_match.group("line"))))
    return events


def _numbered_output_events(record: CommandRecord) -> list[RegionEvent]:
    if not re.search(r"\b(cat|nl)\b", record.command):
        return []
    paths = _command_paths(record.command)
    if len(paths) != 1:
        return []
    line_numbers: list[int] = []
    for line in record.output.splitlines():
        match = _NUMBERED_OUTPUT_RE.match(line)
        if match:
            line_numbers.append(int(match.group("line")))
    if not line_numbers:
        return []
    return [
        RegionEvent(
            event_type="read",
            path=paths[0],
            start_line=min(line_numbers),
            end_line=max(line_numbers),
            label=record.label,
            step=record.step,
            command=record.command,
        )
    ]


def _line_event(record: CommandRecord, path: str, line_no: int) -> RegionEvent:
    return RegionEvent(
        event_type="read",
        path=path,
        start_line=line_no,
        end_line=line_no,
        label=record.label,
        step=record.step,
        command=record.command,
    )


def _looks_like_grep(command: str) -> bool:
    return bool(re.search(r"\bgrep\b", command))


def _command_paths(command: str) -> list[str]:
    paths: list[str] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        for match in _PATH_RE.finditer(token):
            path = _normalize_path(match.group("path"))
            if path and path not in paths:
                paths.append(path)
    return paths


def _normalize_path(raw: str) -> str | None:
    path = raw.strip().strip("'\"").rstrip(":,")
    if not path or path.startswith("-"):
        return None
    if path.startswith("/testbed/"):
        path = path[len("/testbed/") :]
    elif path.startswith("/"):
        return None
    path = path.lstrip("./")
    if not path or path.startswith(("tmp/", "var/")):
        return None
    if not _ALLOWED_PATH_RE.fullmatch(path):
        return None
    return path


def _dedupe_events(events: list[RegionEvent]) -> list[RegionEvent]:
    seen: set[tuple[str, str, int | None, int | None, int | None]] = set()
    deduped: list[RegionEvent] = []
    for event in events:
        key = (
            event.event_type,
            event.path,
            event.start_line,
            event.end_line,
            event.step,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped


def _events_to_region_map(events: list[RegionEvent]) -> RegionMap:
    regions: RegionMap = {}
    for event in events:
        regions.setdefault(event.path, []).append((event.start_line, event.end_line))
    return regions


def _union_region_maps(left: RegionMap, right: RegionMap) -> RegionMap:
    result: RegionMap = {path: list(intervals) for path, intervals in left.items()}
    for path, intervals in right.items():
        result.setdefault(path, []).extend(intervals)
    return _merge_region_map(result)


def _merge_region_map(region_map: RegionMap) -> RegionMap:
    return {
        path: _merge_intervals(intervals)
        for path, intervals in sorted(region_map.items())
    }


def _merge_intervals(intervals: list[Interval]) -> list[Interval]:
    if not intervals:
        return []
    if any(start is None or end is None for start, end in intervals):
        return [(None, None)]
    ordered = sorted((int(start), int(end)) for start, end in intervals)
    merged: list[Interval] = []
    for start, end in ordered:
        if not merged or merged[-1][1] is None or start > int(merged[-1][1]) + 1:
            merged.append((start, end))
            continue
        prev_start, prev_end = merged[-1]
        merged[-1] = (prev_start, max(int(prev_end), end))
    return merged


def _intersect_region_maps(region_maps: list[RegionMap]) -> RegionMap:
    if not region_maps:
        return {}
    common_paths = set(region_maps[0])
    for region_map in region_maps[1:]:
        common_paths &= set(region_map)
    result: RegionMap = {}
    for path in sorted(common_paths):
        intervals = region_maps[0][path]
        for region_map in region_maps[1:]:
            intervals = _intersect_intervals(intervals, region_map[path])
            if not intervals:
                break
        if intervals:
            result[path] = intervals
    return result


def _intersect_intervals(left: list[Interval], right: list[Interval]) -> list[Interval]:
    if _is_full(left):
        return right
    if _is_full(right):
        return left
    result: list[Interval] = []
    for left_start, left_end in left:
        for right_start, right_end in right:
            if (
                left_start is None
                or left_end is None
                or right_start is None
                or right_end is None
            ):
                continue
            start = max(left_start, right_start)
            end = min(left_end, right_end)
            if start <= end:
                result.append((start, end))
    return _merge_intervals(result)


def _subtract_region_maps(base: RegionMap, remove: RegionMap) -> RegionMap:
    result: RegionMap = {}
    for path, intervals in base.items():
        remaining = _subtract_intervals(intervals, remove.get(path, []))
        if remaining:
            result[path] = remaining
    return result


def _subtract_intervals(base: list[Interval], remove: list[Interval]) -> list[Interval]:
    if not remove:
        return base
    if _is_full(remove):
        return []
    if _is_full(base):
        return []
    remaining = [
        (int(start), int(end))
        for start, end in base
        if start is not None and end is not None
    ]
    for rem_start, rem_end in remove:
        if rem_start is None or rem_end is None:
            return []
        next_remaining: list[tuple[int, int]] = []
        for start, end in remaining:
            if rem_end < start or rem_start > end:
                next_remaining.append((start, end))
                continue
            if start < rem_start:
                next_remaining.append((start, rem_start - 1))
            if rem_end < end:
                next_remaining.append((rem_end + 1, end))
        remaining = next_remaining
    return _merge_intervals([(start, end) for start, end in remaining])


def _is_full(intervals: list[Interval]) -> bool:
    return len(intervals) == 1 and (intervals[0][0] is None or intervals[0][1] is None)


def _intersect_lists(lists: list[list[str]]) -> list[str]:
    if not lists:
        return []
    result = set(lists[0])
    for items in lists[1:]:
        result &= set(items)
    return sorted(result)


def _json_region_map(region_map: RegionMap) -> dict[str, list[list[int | None]]]:
    return {
        path: [[start, end] for start, end in intervals]
        for path, intervals in sorted(region_map.items())
    }
