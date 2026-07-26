from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from harbor.models.job.config import DatasetConfig, JobConfig, RetryConfig
from harbor.models.trial.config import AgentConfig
from harbor.swe_touch.records import SCHEMA_VERSION
from harbor.swe_touch.tasks import resolve_task, task_index


ARTIFACT_NAME = "swe_touch_candidate.json"
TASK_INSTRUCTION_PATH = (
    Path(__file__).with_name("prompts") / "counter_edit_synthesis_task_instruction.txt"
)


def prepare_synthesis(
    *,
    tasks_dir: Path,
    regions_path: Path,
    output_dir: Path,
    model: str,
    concurrency: int = 4,
    step_limit: int = 40,
) -> dict[str, Any]:
    records = _read_region_records(regions_path)
    derived_tasks = output_dir / "tasks"
    derived_tasks.mkdir(parents=True, exist_ok=True)
    tasks = task_index(tasks_dir)
    prepared: list[str] = []
    for record in records:
        instance_id = record["instance_id"]
        source = resolve_task(tasks, instance_id)
        target = derived_tasks / instance_id
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        original_instruction = (target / "instruction.md").read_text(encoding="utf-8")
        (target / "instruction.md").write_text(
            _synthesis_instruction(original_instruction, record), encoding="utf-8"
        )
        _copy_reference_repair(target)
        prepared.append(instance_id)

    config = JobConfig(
        job_name=f"swe-touch-synthesis__{_safe_name(model)}",
        jobs_dir=(output_dir / "jobs").resolve(),
        n_concurrent_trials=concurrency,
        retry=RetryConfig(max_retries=2),
        agents=[
            AgentConfig(
                name="mini-swe-agent-external",
                model_name=model,
                max_timeout_sec=10_000,
                kwargs={
                    "step_limit": step_limit,
                    "command_timeout_sec": 600,
                    "model_request_timeout_sec": 10_000,
                    "model_max_retries": 1000,
                    "swe_touch_auto_upload_eval": True,
                },
            )
        ],
        datasets=[DatasetConfig(path=derived_tasks.resolve())],
    )
    config_path = output_dir / "synthesis_job.json"
    config_path.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "model": model,
        "tasks": prepared,
        "job_config": str(config_path),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def collect_synthesis(jobs_dir: Path, output: Path) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for result_path in sorted(jobs_dir.rglob("result.json")):
        trial_dir = result_path.parent
        artifact_paths = sorted(trial_dir.rglob(ARTIFACT_NAME))
        if not artifact_paths:
            failures.append({"trial": str(trial_dir), "reason": "artifact_missing"})
            continue
        try:
            payload = json.loads(artifact_paths[0].read_text(encoding="utf-8"))
            _validate_candidate(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            failures.append({"trial": str(trial_dir), "reason": str(exc)})
            continue
        candidates.append(payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "candidates": candidates,
        "failures": failures,
    }
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "candidates": len(candidates),
        "failures": len(failures),
        "output": str(output),
    }


def build_gate_requests(
    candidates_path: Path, tasks_dir: Path, output: Path
) -> dict[str, Any]:
    payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    tasks = task_index(tasks_dir)
    rows = []
    for candidate in payload.get("candidates") or []:
        _validate_candidate(candidate)
        instance_id = str(candidate["instance_id"])
        rows.append(
            {
                "instance_id": instance_id,
                "candidate_id": str(candidate["candidate_id"]),
                "task_path": str(resolve_task(tasks, instance_id).resolve()),
                "candidate_diff": str(candidate["diff"]),
            }
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return {"requests": len(rows), "output": str(output)}


def _synthesis_instruction(original: str, record: dict[str, Any]) -> str:
    regions = "\n".join(
        f"- {region['path']}:{region['start_line']}-{region['end_line']}"
        for region in record["task_critical_regions"]
    )
    task_instruction = TASK_INSTRUCTION_PATH.read_text(encoding="utf-8").format(
        regions=regions,
        instance_id=record["instance_id"],
    )
    return f"{original.rstrip()}\n\n{task_instruction}"


def _copy_reference_repair(task_dir: Path) -> None:
    solution_dir = task_dir / "solution"
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    solve_path = solution_dir / "solve.sh"
    if not solve_path.exists():
        raise FileNotFoundError(f"reference repair script not found: {solve_path}")
    reference_dir = tests_dir / "reference_repair"
    if reference_dir.exists():
        shutil.rmtree(reference_dir)
    shutil.copytree(solution_dir, reference_dir)


def _validate_candidate(payload: dict[str, Any]) -> None:
    required = {
        "instance_id",
        "candidate_id",
        "diff",
        "target_regions",
        "user_message",
        "wrong_belief",
        "why_it_looks_plausible",
        "expected_failure_mode",
        "self_check",
    }
    missing = required - payload.keys()
    if missing:
        raise ValueError(f"candidate artifact missing fields: {sorted(missing)}")
    if not str(payload["diff"]).startswith("diff --git "):
        raise ValueError("candidate diff is not a unified git diff")
    if not isinstance(payload["target_regions"], list) or not payload["target_regions"]:
        raise ValueError("candidate target_regions is empty")


def _read_region_records(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        files = sorted(path.glob("*.json"))
        records = [json.loads(item.read_text(encoding="utf-8")) for item in files]
    elif path.suffix == ".jsonl":
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        records = [json.loads(path.read_text(encoding="utf-8"))]
    normalized = []
    for record in records:
        regions = record.get("task_critical_regions") or record.get("regions")
        if (
            not record.get("instance_id")
            or not isinstance(regions, list)
            or not regions
        ):
            raise ValueError("region record requires instance_id and non-empty regions")
        normalized.append({**record, "task_critical_regions": regions})
    return normalized


def _safe_name(value: str) -> str:
    return "".join(
        character if character.isalnum() else "-" for character in value
    ).strip("-")
