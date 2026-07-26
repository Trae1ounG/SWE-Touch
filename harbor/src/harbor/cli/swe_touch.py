from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from typer import Argument, Option, Typer

from harbor.swe_touch.aggregate import aggregate_results
from harbor.swe_touch.artifacts import (
    assemble_dataset_from_pipeline,
    assemble_record,
    build_dataset,
)
from harbor.swe_touch.dataset import download_dataset
from harbor.swe_touch.gate import collect_gate, prepare_gate
from harbor.swe_touch.io import read_json, write_json
from harbor.swe_touch.jobs import run_job_configs, write_paired_job_configs
from harbor.swe_touch.regions import mine_critical_regions
from harbor.swe_touch.records import materialize_scenarios, read_records
from harbor.swe_touch.synthesis import (
    build_gate_requests,
    collect_synthesis,
    prepare_synthesis,
)
from harbor.swe_touch.tasks import resolve_task_names
from harbor.swe_touch.validation import validate_path


swe_touch_app = Typer(
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@swe_touch_app.command("download")
def download(
    repo_id: Annotated[str, Option("--repo-id", help="Hugging Face dataset repo ID.")],
    output: Annotated[Path, Option("--output", "-o")],
    revision: Annotated[str | None, Option("--revision")] = None,
) -> None:
    _print(download_dataset(repo_id=repo_id, output_dir=output, revision=revision))


@swe_touch_app.command("validate-data")
def validate_data(
    records: Annotated[Path, Argument(help="Released JSONL file or directory.")],
) -> None:
    _print(validate_path(records))


@swe_touch_app.command("mine-regions")
def mine_regions(
    trajectories: Annotated[Path, Option("--trajectories")],
    output: Annotated[Path, Option("--output", "-o")],
    minimum_models: Annotated[int, Option("--minimum-models", min=1)] = 2,
    max_regions: Annotated[int, Option("--max-regions", min=1)] = 8,
    reference_patch: Annotated[Path | None, Option("--reference-patch")] = None,
) -> None:
    payload = read_json(trajectories)
    if not isinstance(payload, dict) or not payload.get("instance_id"):
        raise ValueError("trajectory input must contain instance_id")
    rows = payload.get("trajectories")
    if not isinstance(rows, list):
        raise TypeError("trajectory input must contain a trajectories array")
    regions = mine_critical_regions(
        rows,
        minimum_models=minimum_models,
        max_regions=max_regions,
        reference_patch=(
            reference_patch.read_text(encoding="utf-8") if reference_patch else None
        ),
    )
    result = {
        "schema_version": "1.0.0",
        "benchmark": payload.get("benchmark"),
        "instance_id": payload["instance_id"],
        "regions": regions,
        "evidence": {"source": "trajectory_edit_overlap"},
    }
    write_json(output, result)
    _print({"regions": len(regions), "output": str(output)})


@swe_touch_app.command("assemble-record")
def assemble_record_command(
    benchmark: Annotated[str, Option("--benchmark")],
    instance_id: Annotated[str, Option("--instance-id")],
    regions: Annotated[Path, Option("--regions")],
    candidates: Annotated[Path, Option("--candidates")],
    gates: Annotated[Path, Option("--gates")],
    output: Annotated[Path, Option("--output", "-o")],
    trigger_event: Annotated[str, Option("--trigger-event")] = "read_or_edit",
    max_interventions: Annotated[int, Option("--max-interventions", min=1)] = 3,
) -> None:
    _print(
        assemble_record(
            benchmark=benchmark,
            instance_id=instance_id,
            regions_path=regions,
            candidates_path=candidates,
            gates_path=gates,
            output_path=output,
            trigger_event=trigger_event,
            max_interventions=max_interventions,
            prompt_id="counter_edit_user_simulator",
        )
    )


@swe_touch_app.command("assemble-dataset")
def assemble_dataset(
    regions: Annotated[Path, Option("--regions")],
    candidates: Annotated[Path, Option("--candidates")],
    gates: Annotated[Path, Option("--gates")],
    output: Annotated[Path, Option("--output", "-o")],
    trigger_event: Annotated[str, Option("--trigger-event")] = "read_or_edit",
    max_interventions: Annotated[int, Option("--max-interventions", min=1)] = 3,
) -> None:
    _print(
        assemble_dataset_from_pipeline(
            regions_path=regions,
            candidates_path=candidates,
            gates_path=gates,
            output_path=output,
            trigger_event=trigger_event,
            max_interventions=max_interventions,
            prompt_id="counter_edit_user_simulator",
        )
    )


@swe_touch_app.command("build-dataset")
def build_dataset_command(
    inputs: Annotated[list[Path], Option("--input")],
    output: Annotated[Path, Option("--output", "-o")],
) -> None:
    _print(build_dataset(inputs, output))


@swe_touch_app.command("materialize")
def materialize(
    records: Annotated[
        Path, Argument(help="Released SWE-Touch JSONL file or directory.")
    ],
    output: Annotated[
        Path, Option("--output", "-o", help="Scenario output directory.")
    ],
) -> None:
    _print(materialize_scenarios(read_records(records), output))


@swe_touch_app.command("prepare-paired")
def prepare_paired(
    tasks: Annotated[Path, Option("--tasks", help="Harbor task dataset directory.")],
    records: Annotated[Path, Option("--records", help="Released SWE-Touch JSONL.")],
    output: Annotated[Path, Option("--output", "-o")],
    model: Annotated[str, Option("--model")],
    simulator_model: Annotated[
        str, Option("--simulator-model", help="LiteLLM model for user messages.")
    ] = "openai/gpt-4o",
    repetitions: Annotated[int, Option("--repetitions", min=1)] = 1,
    concurrency: Annotated[int, Option("--concurrency", min=1)] = 4,
    step_limit: Annotated[int, Option("--step-limit", min=1)] = 100,
    model_max_tokens: Annotated[int | None, Option("--model-max-tokens", min=1)] = None,
) -> None:
    release_records = read_records(records)
    scenarios = output / "scenarios"
    scenario_manifest = materialize_scenarios(release_records, scenarios)
    task_names = resolve_task_names(
        tasks, (record["instance_id"] for record in release_records)
    )
    configs = write_paired_job_configs(
        tasks_dir=tasks,
        task_names=task_names,
        scenarios_dir=scenarios,
        output_dir=output,
        model=model,
        simulator_model=simulator_model,
        repetitions=repetitions,
        concurrency=concurrency,
        step_limit=step_limit,
        model_max_tokens=model_max_tokens,
    )
    _print(
        {
            "scenarios": scenario_manifest,
            "tasks": task_names,
            "configs": {name: str(path) for name, path in configs.items()},
        }
    )


@swe_touch_app.command("run-paired")
def run_paired(
    directory: Annotated[
        Path,
        Argument(help="Directory created by `harbor swe-touch prepare-paired`."),
    ],
) -> None:
    paths = [directory / "vanilla.json", directory / "counter_edit.json"]
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
    run_job_configs(paths)


@swe_touch_app.command("aggregate")
def aggregate(
    jobs: Annotated[Path, Argument(help="Harbor jobs directory.")],
    output: Annotated[Path, Option("--output", "-o")],
) -> None:
    _print(aggregate_results(jobs, output))


@swe_touch_app.command("prepare-synthesis")
def prepare_synthesis_command(
    tasks: Annotated[Path, Option("--tasks")],
    records: Annotated[Path, Option("--records")],
    output: Annotated[Path, Option("--output", "-o")],
    model: Annotated[str, Option("--model")] = "openai/gpt-5.5",
    concurrency: Annotated[int, Option("--concurrency", min=1)] = 4,
    step_limit: Annotated[int, Option("--step-limit", min=1)] = 40,
) -> None:
    _print(
        prepare_synthesis(
            tasks_dir=tasks,
            regions_path=records,
            output_dir=output,
            model=model,
            concurrency=concurrency,
            step_limit=step_limit,
        )
    )


@swe_touch_app.command("collect-synthesis")
def collect_synthesis_command(
    jobs: Annotated[Path, Argument()],
    output: Annotated[Path, Option("--output", "-o")],
) -> None:
    _print(collect_synthesis(jobs, output))


@swe_touch_app.command("prepare-gate")
def prepare_gate_command(
    requests: Annotated[Path, Option("--requests")],
    output: Annotated[Path, Option("--output", "-o")],
    concurrency: Annotated[int, Option("--concurrency", min=1)] = 8,
) -> None:
    _print(
        prepare_gate(requests_path=requests, output_dir=output, concurrency=concurrency)
    )


@swe_touch_app.command("build-gate-requests")
def build_gate_requests_command(
    candidates: Annotated[Path, Option("--candidates")],
    tasks: Annotated[Path, Option("--tasks")],
    output: Annotated[Path, Option("--output", "-o")],
) -> None:
    _print(build_gate_requests(candidates, tasks, output))


@swe_touch_app.command("collect-gate")
def collect_gate_command(
    jobs: Annotated[Path, Option("--jobs")],
    manifest: Annotated[Path, Option("--manifest")],
    output: Annotated[Path, Option("--output", "-o")],
) -> None:
    _print(collect_gate(jobs, manifest, output))


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))
