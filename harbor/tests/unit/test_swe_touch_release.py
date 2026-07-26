from __future__ import annotations

import hashlib
import json
from pathlib import Path

from harbor.models.job.config import JobConfig
from harbor.models.task.task import Task
from harbor.swe_touch.gate import prepare_gate
from harbor.swe_touch.jobs import write_paired_job_configs
from harbor.swe_touch.records import materialize_scenarios, read_records
from harbor.swe_touch.synthesis import TASK_INSTRUCTION_PATH, prepare_synthesis
from harbor.swe_touch.tasks import resolve_task_names
from harbor.swe_touch.runtime.scenario_store import load_scenario_directory
from harbor.swe_touch.runtime.user_simulator import (
    USER_SIMULATOR_PROMPT_SHA256,
    load_counter_edit_user_simulator_prompt,
)


def test_user_simulator_prompt_matches_release_checksum() -> None:
    prompt = load_counter_edit_user_simulator_prompt()
    assert hashlib.sha256(prompt.encode()).hexdigest() == USER_SIMULATOR_PROMPT_SHA256


def test_release_record_materializes_three_runtime_scenarios(tmp_path: Path) -> None:
    record_path = tmp_path / "records.jsonl"
    record_path.write_text(json.dumps(_record()) + "\n", encoding="utf-8")

    manifest = materialize_scenarios(read_records(record_path), tmp_path / "scenarios")
    scenarios = load_scenario_directory(tmp_path / "scenarios")

    assert manifest == {"records": 1, "scenarios": 3}
    assert [scenario.user.intervention_index for scenario in scenarios] == [1, 2, 3]
    assert all(scenario.user.role == "counter_edit_user" for scenario in scenarios)


def test_paired_jobs_differ_only_by_counter_edit_runtime(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    _write_task(tasks / "example__task-1")
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    paths = write_paired_job_configs(
        tasks_dir=tasks,
        task_names=["example__task-1"],
        scenarios_dir=scenarios,
        output_dir=tmp_path / "run",
        model="openai/example-model",
        simulator_model="openai/gpt-4o",
        repetitions=3,
        concurrency=5,
    )
    vanilla = JobConfig.model_validate_json(paths["vanilla"].read_text())
    counter = JobConfig.model_validate_json(paths["counter_edit"].read_text())

    assert vanilla.n_attempts == counter.n_attempts == 3
    assert vanilla.n_concurrent_trials == counter.n_concurrent_trials == 5
    assert vanilla.agents[0].model_name == counter.agents[0].model_name
    assert vanilla.datasets[0].task_names == ["example__task-1"]
    assert counter.datasets[0].task_names == ["example__task-1"]
    assert "swe_touch_scenarios_path" not in vanilla.agents[0].kwargs
    assert counter.agents[0].kwargs["swe_touch_intervention_mode"] == "patch_message"


def test_release_instance_ids_resolve_exact_public_task_names(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    for name in (
        "astropy__astropy-12907",
        "instance_ansible__ansible-example",
        "arcane-drift-detection-baselines",
    ):
        _write_task(tasks / name)

    assert resolve_task_names(
        tasks,
        [
            "astropy__astropy-12907",
            "swebenchpro_instance_ansible__ansible-example",
            "deepswe_arcane-drift-detection-baselines",
        ],
    ) == [
        "astropy__astropy-12907",
        "instance_ansible__ansible-example",
        "arcane-drift-detection-baselines",
    ]


def test_release_instance_id_does_not_match_by_substring(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    _write_task(tasks / "prefix-example__task-1-suffix")

    try:
        resolve_task_names(tasks, ["example__task-1"])
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("task matching must not use substring matches")


def test_synthesis_and_gate_prepare_native_harbor_tasks(tmp_path: Path) -> None:
    tasks = tmp_path / "source_tasks"
    source_task = tasks / "example__task-1"
    _write_task(source_task)
    records = tmp_path / "records.jsonl"
    records.write_text(json.dumps(_record()) + "\n", encoding="utf-8")

    synthesis = prepare_synthesis(
        tasks_dir=tasks,
        regions_path=records,
        output_dir=tmp_path / "synthesis",
        model="openai/gpt-5.5",
    )
    derived = tmp_path / "synthesis" / "tasks" / "example__task-1"
    assert synthesis["tasks"] == ["example__task-1"]
    assert Task.is_valid_dir(derived)
    assert "/tests/swe_touch_eval.sh" in (derived / "instruction.md").read_text()
    assert (derived / "tests" / "reference_repair" / "solve.sh").exists()

    requests = tmp_path / "gate_requests.jsonl"
    requests.write_text(
        json.dumps(
            {
                "instance_id": "example__task-1",
                "candidate_id": "candidate-1",
                "task_path": str(source_task),
                "candidate_diff": _diff(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    gate = prepare_gate(requests_path=requests, output_dir=tmp_path / "gate")
    assert gate["tasks"] == 3
    for task_dir in (tmp_path / "gate" / "tasks").iterdir():
        assert Task.is_valid_dir(task_dir)


def test_synthesis_uses_versioned_task_instruction_without_system_prompt_override() -> (
    None
):
    instruction = TASK_INSTRUCTION_PATH.read_text(encoding="utf-8")

    assert "/logs/artifacts/swe_touch_candidate.json" in instruction
    assert "{regions}" in instruction
    assert "{instance_id}" in instruction
    assert "system prompt" not in instruction.lower()


def _record() -> dict:
    patch = {
        "id": "candidate-1",
        "diff": _diff(),
        "target_regions": [{"path": "source.py", "start_line": 1, "end_line": 1}],
        "user_claim": "Keep this branch as written.",
    }
    intervention = {
        "delivery": "patch_and_message",
        "patch": patch,
        "trigger": {
            "event": "read_or_edit",
            "max_triggers": 1,
            "regions": patch["target_regions"],
        },
    }
    return {
        "schema_version": "1.0.0",
        "benchmark": "swe_bench_verified",
        "split": "test",
        "instance_id": "example__task-1",
        "task_critical_regions": patch["target_regions"],
        "counter_edit": {
            "mode": "patch",
            "max_interventions": 3,
            "message_prompt_id": "counter_edit_user_simulator",
            "interventions": [
                {"order": order, **intervention} for order in range(1, 4)
            ],
            "validation": {"status": "validated_counter_edit"},
        },
    }


def _diff() -> str:
    return (
        "diff --git a/source.py b/source.py\n"
        "--- a/source.py\n"
        "+++ b/source.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
    )


def _write_task(task_dir: Path) -> None:
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "solution").mkdir()
    (task_dir / "tests").mkdir()
    (task_dir / "task.toml").write_text(
        """version = "1.0"
[task]
name = "example/task-1"
authors = []
keywords = []
[verifier]
timeout_sec = 60.0
[agent]
timeout_sec = 60.0
[environment]
build_timeout_sec = 60.0
cpus = 1
memory_mb = 1024
storage_mb = 1024
gpus = 0
""",
        encoding="utf-8",
    )
    (task_dir / "instruction.md").write_text("Fix the example bug.\n", encoding="utf-8")
    (task_dir / "environment" / "Dockerfile").write_text(
        "FROM alpine:3.20\nWORKDIR /testbed\nRUN apk add --no-cache git bash patch\n"
        "RUN git init && printf 'value = 1\\n' > source.py && git add source.py && git commit -m init\n",
        encoding="utf-8",
    )
    (task_dir / "solution" / "solve.sh").write_text(
        "#!/usr/bin/env bash\nsed -i 's/value = 1/value = 3/' /testbed/source.py\n",
        encoding="utf-8",
    )
    (task_dir / "tests" / "test.sh").write_text(
        "#!/usr/bin/env bash\ngrep -q 'value = 3' /testbed/source.py && echo 1 > /logs/verifier/reward.txt || echo 0 > /logs/verifier/reward.txt\n",
        encoding="utf-8",
    )
