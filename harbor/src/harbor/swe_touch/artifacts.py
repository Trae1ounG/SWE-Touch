from __future__ import annotations

from pathlib import Path
from typing import Any

from harbor.swe_touch.records import SCHEMA_VERSION
from harbor.swe_touch.io import read_json, read_records, write_jsonl, write_record
from harbor.swe_touch.regions import normalize_regions
from harbor.swe_touch.validation import validate_record


def assemble_record(
    *,
    benchmark: str,
    instance_id: str,
    regions_path: Path,
    candidates_path: Path,
    gates_path: Path,
    output_path: Path,
    trigger_event: str,
    max_interventions: int,
    prompt_id: str,
) -> dict[str, Any]:
    """Assemble pipeline artifacts into the exact public evaluation record."""

    region_artifact = read_json(regions_path)
    candidate_artifact = read_json(candidates_path)
    gate_artifact = read_json(gates_path)
    record = assemble_record_payload(
        benchmark=benchmark,
        instance_id=instance_id,
        region_artifact=region_artifact,
        candidate_artifact=candidate_artifact,
        gate_artifact=gate_artifact,
        trigger_event=trigger_event,
        max_interventions=max_interventions,
        prompt_id=prompt_id,
    )
    write_record(output_path, record)
    return record


def assemble_record_payload(
    *,
    benchmark: str,
    instance_id: str,
    region_artifact: dict[str, Any],
    candidate_artifact: dict[str, Any],
    gate_artifact: dict[str, Any],
    trigger_event: str,
    max_interventions: int,
    prompt_id: str,
) -> dict[str, Any]:
    _validate_pipeline_artifacts(
        instance_id=instance_id,
        regions=region_artifact,
        candidates=candidate_artifact,
        gates=gate_artifact,
    )
    regions = region_artifact.get("regions") or []
    candidates = candidate_artifact.get("candidates") or []
    gate_rows = gate_artifact.get("candidates") or []
    gates = {str(row["candidate_id"]): row for row in gate_rows}

    accepted = [
        row
        for row in candidates
        if _passes_validation(gates.get(str(row.get("candidate_id")), {}))
    ]
    if not accepted:
        counter_edit = {
            "mode": "text_only",
            "max_interventions": max_interventions,
            "message_prompt_id": prompt_id,
            "interventions": [
                {
                    "order": order,
                    "trigger": {
                        "event": trigger_event,
                        "regions": regions,
                        "max_triggers": 1,
                    },
                    "delivery": "message_only",
                }
                for order in range(1, max_interventions + 1)
            ],
            "validation": {
                "status": "no_accepted_counter_edit",
                "passes_validation": False,
            },
            "fallback_reason": "no_candidate_passed_validation",
        }
    else:
        selected = accepted[:max_interventions]
        while len(selected) < max_interventions:
            selected.append(selected[-1])
        interventions = []
        for order, candidate in enumerate(selected, 1):
            candidate_id = str(candidate["candidate_id"])
            gate = gates[candidate_id]
            target_regions = (
                normalize_regions(candidate.get("target_regions")) or regions
            )
            interventions.append(
                {
                    "order": order,
                    "trigger": {
                        "event": trigger_event,
                        "regions": target_regions,
                        "max_triggers": 1,
                    },
                    "delivery": "patch_and_message",
                    "patch": {
                        "id": candidate_id,
                        "diff": candidate["diff"],
                        "target_regions": target_regions,
                        "user_claim": candidate.get("user_message"),
                        "mistaken_belief": candidate.get("wrong_belief"),
                        "plausibility": candidate.get("why_it_looks_plausible"),
                        "expected_failure": candidate.get("expected_failure_mode"),
                    },
                    "validation": _public_gate(gate),
                }
            )
        counter_edit = {
            "mode": "patch",
            "max_interventions": max_interventions,
            "message_prompt_id": prompt_id,
            "interventions": interventions,
            "validation": {
                "status": "validated_counter_edit",
                "all_interventions_validated": True,
            },
        }

    record = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": benchmark,
        "split": "test",
        "instance_id": instance_id,
        "task_critical_regions": regions,
        "region_evidence": region_artifact.get("evidence") or {},
        "counter_edit": counter_edit,
        "controls": {},
    }
    validate_record(record)
    return record


def assemble_dataset_from_pipeline(
    *,
    regions_path: Path,
    candidates_path: Path,
    gates_path: Path,
    output_path: Path,
    trigger_event: str = "read_or_edit",
    max_interventions: int = 3,
    prompt_id: str = "counter_edit_user_simulator",
) -> dict[str, Any]:
    regions = _load_region_artifacts(regions_path)
    candidates_payload = read_json(candidates_path)
    gates_payload = read_json(gates_path)
    candidate_rows = candidates_payload.get("candidates") or []
    gate_rows = gates_payload.get("candidates") or []
    candidates_by_instance: dict[str, list[dict[str, Any]]] = {}
    gates_by_instance: dict[str, list[dict[str, Any]]] = {}
    for row in candidate_rows:
        candidates_by_instance.setdefault(str(row["instance_id"]), []).append(row)
    for row in gate_rows:
        gates_by_instance.setdefault(str(row["instance_id"]), []).append(row)

    records = []
    for instance_id, region_artifact in sorted(regions.items()):
        candidate_artifact = {
            "schema_version": SCHEMA_VERSION,
            "instance_id": instance_id,
            "candidates": candidates_by_instance.get(instance_id, []),
        }
        gate_artifact = {
            "schema_version": SCHEMA_VERSION,
            "instance_id": instance_id,
            "candidates": gates_by_instance.get(instance_id, []),
        }
        records.append(
            assemble_record_payload(
                benchmark=str(region_artifact["benchmark"]),
                instance_id=instance_id,
                region_artifact=region_artifact,
                candidate_artifact=candidate_artifact,
                gate_artifact=gate_artifact,
                trigger_event=trigger_event,
                max_interventions=max_interventions,
                prompt_id=prompt_id,
            )
        )
    write_jsonl(output_path, records)
    return {"records": len(records), "output": str(output_path)}


def _load_region_artifacts(path: Path) -> dict[str, dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if path.is_dir():
        artifacts = [read_json(item) for item in sorted(path.glob("*.json"))]
    elif path.suffix == ".jsonl":
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                import json

                artifacts.append(json.loads(line))
    else:
        artifacts = [read_json(path)]
    result = {}
    for artifact in artifacts:
        instance_id = str(artifact.get("instance_id") or "")
        if not instance_id:
            raise ValueError("critical-region artifact is missing instance_id")
        artifact.setdefault("schema_version", SCHEMA_VERSION)
        if "regions" not in artifact and "task_critical_regions" in artifact:
            artifact["regions"] = artifact["task_critical_regions"]
        if "benchmark" not in artifact:
            raise ValueError(
                f"{instance_id}: critical-region artifact is missing benchmark"
            )
        result[instance_id] = artifact
    return result


def build_dataset(inputs: list[Path], output_path: Path) -> dict[str, Any]:
    """Collect final records into release JSONL without schema translation."""

    records: dict[tuple[str, str], dict[str, Any]] = {}
    for path in inputs:
        for record in read_records(path):
            validate_record(record)
            key = (str(record["benchmark"]), str(record["instance_id"]))
            if key in records:
                raise ValueError(f"duplicate dataset record: {key[0]}::{key[1]}")
            records[key] = record
    ordered = [records[key] for key in sorted(records)]
    write_jsonl(output_path, ordered)
    return {"records": len(ordered), "output": str(output_path)}


def _public_gate(gate: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "validated_counter_edit" if _passes_validation(gate) else "rejected",
        "passes_validation": _passes_validation(gate),
        "reference_only": gate.get("reference_only"),
        "user_edit_only": gate.get("user_edit_only"),
        "user_edit_plus_reference": gate.get("user_edit_plus_reference"),
    }


def _passes_validation(gate: dict[str, Any]) -> bool:
    return (
        _resolved(gate.get("reference_only")) is True
        and _resolved(gate.get("user_edit_only")) is False
        and _resolved(gate.get("user_edit_plus_reference")) is False
    )


def _resolved(outcome: Any) -> bool | None:
    if not isinstance(outcome, dict):
        return None
    if isinstance(outcome.get("resolved"), bool):
        return outcome["resolved"]
    reward = outcome.get("reward")
    if isinstance(reward, int | float):
        return reward >= 1
    return None


def _validate_pipeline_artifacts(
    *,
    instance_id: str,
    regions: Any,
    candidates: Any,
    gates: Any,
) -> None:
    for name, artifact in {
        "critical regions": regions,
        "candidates": candidates,
        "gate results": gates,
    }.items():
        if not isinstance(artifact, dict):
            raise TypeError(f"{name} artifact must be a JSON object")
        if artifact.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"{name} artifact has unsupported schema version")
        artifact_instance = artifact.get("instance_id")
        if artifact_instance != instance_id:
            raise ValueError(
                f"{name} artifact instance mismatch: {artifact_instance!r} != {instance_id!r}"
            )

    normalized_regions = regions.get("regions")
    if not isinstance(normalized_regions, list) or not normalized_regions:
        raise ValueError("critical regions artifact must contain at least one region")

    candidate_rows = candidates.get("candidates")
    gate_rows = gates.get("candidates")
    if not isinstance(candidate_rows, list) or not isinstance(gate_rows, list):
        raise TypeError("candidate and gate artifacts must contain candidates arrays")
    candidate_ids: set[str] = set()
    for row in candidate_rows:
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id or candidate_id in candidate_ids:
            raise ValueError(f"invalid or duplicate candidate_id: {candidate_id!r}")
        candidate_ids.add(candidate_id)
        if not str(row.get("diff") or "").startswith("diff --git "):
            raise ValueError(
                f"candidate {candidate_id}: diff is not a unified git diff"
            )
        if not normalize_regions(row.get("target_regions")):
            raise ValueError(f"candidate {candidate_id}: target_regions is empty")

    gate_ids: set[str] = set()
    for row in gate_rows:
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id or candidate_id in gate_ids:
            raise ValueError(
                f"invalid or duplicate gate candidate_id: {candidate_id!r}"
            )
        gate_ids.add(candidate_id)
    unknown_gate_ids = gate_ids - candidate_ids
    if unknown_gate_ids:
        raise ValueError(
            f"gate results reference unknown candidates: {sorted(unknown_gate_ids)}"
        )
