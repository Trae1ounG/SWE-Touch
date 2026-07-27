from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0.0"
USER_SIMULATOR_PROMPT_ID = "counter_edit_user_simulator"


def read_records(path: Path) -> list[dict[str, Any]]:
    files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    records: list[dict[str, Any]] = []
    for file_path in files:
        for line_number, line in enumerate(
            file_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{file_path}:{line_number}: invalid JSON") from exc
            validate_record(record)
            records.append(record)
    return records


def validate_record(record: dict[str, Any]) -> None:
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported SWE-Touch record schema")
    instance_id = record.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("record requires a non-empty instance_id")
    regions = record.get("task_critical_regions")
    if not isinstance(regions, list) or not regions:
        raise ValueError(f"{instance_id}: task_critical_regions is empty")
    for region in regions:
        _validate_region(instance_id, region)
    counter_edit = record.get("counter_edit")
    if not isinstance(counter_edit, dict):
        raise ValueError(f"{instance_id}: counter_edit is missing")
    prompt_id = counter_edit.get("message_prompt_id")
    if prompt_id != USER_SIMULATOR_PROMPT_ID:
        raise ValueError(
            f"{instance_id}: expected {USER_SIMULATOR_PROMPT_ID}, got {prompt_id!r}"
        )
    interventions = counter_edit.get("interventions")
    if not isinstance(interventions, list) or not interventions:
        raise ValueError(f"{instance_id}: counter_edit.interventions is empty")
    for intervention in interventions:
        order = intervention.get("order")
        if not isinstance(order, int) or order < 1:
            raise ValueError(f"{instance_id}: invalid intervention order {order!r}")
        trigger = intervention.get("trigger") or {}
        if order > 1 and trigger.get("event") != "edit":
            raise ValueError(
                f"{instance_id}: intervention {order} must wait for a code edit"
            )
        for region in trigger.get("regions") or []:
            _validate_region(instance_id, region)
        patch = intervention.get("patch")
        if patch is not None and not str(patch.get("diff") or "").startswith(
            "diff --git "
        ):
            raise ValueError(f"{instance_id}: intervention patch is not a git diff")


def materialize_scenarios(
    records: Iterable[dict[str, Any]] | Path,
    output: Path,
) -> dict[str, int]:
    if isinstance(records, Path):
        records = read_records(records)
    output.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, str]] = []
    record_count = 0
    for record in records:
        validate_record(record)
        record_count += 1
        instance_id = record["instance_id"]
        instance_dir = output / instance_id
        instance_dir.mkdir(parents=True, exist_ok=True)
        counter_edit = record["counter_edit"]
        for intervention in counter_edit["interventions"]:
            order = int(intervention["order"])
            patch = intervention.get("patch")
            patch_block = None
            if patch:
                diff_name = f"user_edit_{order}.diff"
                (instance_dir / diff_name).write_text(patch["diff"], encoding="utf-8")
                patch_block = {
                    "patch_id": patch["id"],
                    "patch_kind": "counter_edit",
                    "patch_source": "release_record",
                    "diff_path": diff_name,
                }
            regions = [
                _trigger_region(region, intervention)
                for region in intervention["trigger"]["regions"]
            ]
            trigger: dict[str, Any]
            if len(regions) == 1:
                trigger = regions[0]
            else:
                trigger = {
                    "type": "code_regions",
                    "regions": regions,
                    "event": intervention["trigger"]["event"],
                    "min_overlap_lines": 1,
                }
            scenario = {
                "scenario_id": f"counter_edit__{instance_id}__r{order}",
                "instance_id": instance_id,
                "trigger": trigger,
                "patch": patch_block,
                "user": {
                    "role": "counter_edit_user",
                    "message": (patch or {}).get("user_claim")
                    or "I already checked this implementation. Please keep it and build from here.",
                    "message_source": "user_simulator_seed",
                    "message_prompt_id": USER_SIMULATOR_PROMPT_ID,
                    "message_model": None,
                    "intervention_index": order,
                },
                "policy": {
                    "policy_id": "counter_edit",
                    "trigger_strategy": f"{intervention['trigger']['event']}_critical_region",
                    "validation_status": counter_edit.get("validation", {}).get(
                        "status"
                    ),
                },
                "max_triggers": int(intervention["trigger"].get("max_triggers") or 1),
            }
            relative_path = Path(instance_id) / f"round{order}.json"
            _write_json(output / relative_path, scenario)
            index.append({"path": relative_path.as_posix()})
    _write_json(output / "index.json", {"scenarios": index})
    manifest = {"records": record_count, "scenarios": len(index)}
    _write_json(output / "manifest.json", manifest)
    return manifest


def _trigger_region(
    region: dict[str, Any], intervention: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "code_region",
        "path": region["path"],
        "start_line": int(region["start_line"]),
        "end_line": int(region["end_line"]),
        "event": intervention["trigger"]["event"],
        "min_overlap_lines": 1,
    }


def _validate_region(instance_id: str, region: Any) -> None:
    if not isinstance(region, dict):
        raise ValueError(f"{instance_id}: invalid region")
    path = region.get("path")
    start = region.get("start_line")
    end = region.get("end_line")
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise ValueError(f"{instance_id}: region path must be repository-relative")
    if (
        not isinstance(start, int)
        or not isinstance(end, int)
        or start < 1
        or end < start
    ):
        raise ValueError(f"{instance_id}: invalid region span {path}:{start}-{end}")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
