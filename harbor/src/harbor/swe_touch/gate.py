from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import toml

from harbor.models.job.config import DatasetConfig, JobConfig, RetryConfig
from harbor.models.trial.config import AgentConfig
from harbor.models.trial.result import TrialResult
from harbor.swe_touch.records import SCHEMA_VERSION


VARIANTS = ("reference_only", "user_edit_only", "user_edit_plus_reference")


def prepare_gate(
    *,
    requests_path: Path,
    output_dir: Path,
    concurrency: int = 8,
) -> dict[str, Any]:
    requests = _read_requests(requests_path)
    tasks_dir = output_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows: list[dict[str, str]] = []
    for request in requests:
        source = Path(request["task_path"]).expanduser().resolve()
        for variant in VARIANTS:
            task_name = _gate_task_name(
                request["instance_id"], request["candidate_id"], variant
            )
            target = tasks_dir / task_name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            _rewrite_task_name(target, task_name)
            _write_gate_solution(target, request, variant)
            manifest_rows.append(
                {
                    "task_name": task_name,
                    "instance_id": request["instance_id"],
                    "candidate_id": request["candidate_id"],
                    "variant": variant,
                }
            )

    config = JobConfig(
        job_name="swe-touch-validation",
        jobs_dir=(output_dir / "jobs").resolve(),
        n_concurrent_trials=concurrency,
        retry=RetryConfig(max_retries=2),
        agents=[AgentConfig(name="oracle")],
        datasets=[DatasetConfig(path=tasks_dir.resolve())],
    )
    config_path = output_dir / "gate_job.json"
    config_path.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tasks": manifest_rows,
        "job_config": str(config_path),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "candidates": len(requests),
        "tasks": len(manifest_rows),
        "job_config": str(config_path),
    }


def collect_gate(jobs_dir: Path, manifest_path: Path, output: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task_map = {row["task_name"]: row for row in manifest["tasks"]}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for result_path in sorted(jobs_dir.rglob("result.json")):
        result = TrialResult.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
        task_name = result.task_id.get_name()
        row = task_map.get(task_name) or task_map.get(Path(task_name).name)
        if row is None:
            continue
        key = (row["instance_id"], row["candidate_id"])
        candidate = grouped.setdefault(
            key,
            {
                "instance_id": row["instance_id"],
                "candidate_id": row["candidate_id"],
            },
        )
        candidate[row["variant"]] = {
            "resolved": _resolved(result),
            "error": result.exception_info.exception_type
            if result.exception_info
            else None,
            "result_path": str(result_path),
        }

    candidates = []
    for candidate in grouped.values():
        candidate["passes_validation"] = (
            _outcome(candidate, "reference_only") is True
            and _outcome(candidate, "user_edit_only") is False
            and _outcome(candidate, "user_edit_plus_reference") is False
        )
        candidates.append(candidate)
    payload = {"schema_version": SCHEMA_VERSION, "candidates": candidates}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "candidates": len(candidates),
        "accepted": sum(row["passes_validation"] for row in candidates),
        "output": str(output),
    }


def _read_requests(path: Path) -> list[dict[str, str]]:
    rows = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        required = {"instance_id", "candidate_id", "task_path", "candidate_diff"}
        missing = required - row.keys()
        if missing:
            raise ValueError(f"{path}:{line_number}: missing {sorted(missing)}")
        rows.append({key: str(value) for key, value in row.items()})
    return rows


def _write_gate_solution(task_dir: Path, request: dict[str, str], variant: str) -> None:
    solution_dir = task_dir / "solution"
    reference_dir = solution_dir / "reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    original_solve = solution_dir / "solve.sh"
    if not original_solve.exists():
        raise FileNotFoundError(f"reference solve script not found: {original_solve}")
    for path in list(solution_dir.iterdir()):
        if path == reference_dir:
            continue
        destination = reference_dir / path.name
        if path.is_dir():
            shutil.copytree(path, destination)
        else:
            shutil.copy2(path, destination)
    (solution_dir / "candidate.diff").write_text(
        request["candidate_diff"], encoding="utf-8"
    )
    steps = []
    if variant in {"user_edit_only", "user_edit_plus_reference"}:
        steps.append(_apply_patch_shell("/solution/candidate.diff"))
    if variant in {"reference_only", "user_edit_plus_reference"}:
        steps.append("bash /solution/reference/solve.sh")
    original_solve.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + _find_repo_shell()
        + "\n"
        + "\n".join(steps)
        + "\n",
        encoding="utf-8",
    )
    original_solve.chmod(0o755)


def _rewrite_task_name(task_dir: Path, task_name: str) -> None:
    config_path = task_dir / "task.toml"
    config = toml.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("task", {})["name"] = f"swe-touch/{task_name}"
    config_path.write_text(toml.dumps(config), encoding="utf-8")


def _find_repo_shell() -> str:
    return 'for root in /testbed /app "$PWD"; do if [ -d "$root/.git" ]; then cd "$root"; break; fi; done'


def _apply_patch_shell(path: str) -> str:
    return f"git apply --whitespace=nowarn {path} || patch --forward --fuzz=5 --batch -p1 -i {path}"


def _gate_task_name(instance_id: str, candidate_id: str, variant: str) -> str:
    return f"{_safe(instance_id)}__gate__{_safe(candidate_id)}__{variant}"


def _safe(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "_.-" else "-"
        for character in value
    )


def _resolved(result: TrialResult) -> bool | None:
    if result.exception_info is not None or result.verifier_result is None:
        return None
    rewards = result.verifier_result.rewards or {}
    if "reward" in rewards:
        return float(rewards["reward"]) >= 1
    if len(rewards) == 1:
        return float(next(iter(rewards.values()))) >= 1
    return None


def _outcome(candidate: dict[str, Any], name: str) -> bool | None:
    outcome = candidate.get(name)
    return outcome.get("resolved") if isinstance(outcome, dict) else None
