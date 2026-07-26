from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from harbor.models.job.config import DatasetConfig, JobConfig, RetryConfig
from harbor.models.trial.config import AgentConfig


def write_paired_job_configs(
    *,
    tasks_dir: Path,
    task_names: list[str],
    scenarios_dir: Path,
    output_dir: Path,
    model: str,
    simulator_model: str,
    repetitions: int = 1,
    concurrency: int = 4,
    step_limit: int = 100,
    agent_timeout_sec: int = 10_000,
    command_timeout_sec: int = 600,
    model_max_tokens: int | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    common_kwargs: dict[str, Any] = {
        "step_limit": step_limit,
        "command_timeout_sec": command_timeout_sec,
        "model_request_timeout_sec": agent_timeout_sec,
        "model_max_retries": 1000,
        "model_retry_initial_sleep_sec": 2,
        "model_retry_max_sleep_sec": 120,
    }
    if model_max_tokens is not None:
        common_kwargs["model_max_tokens"] = model_max_tokens

    paths: dict[str, Path] = {}
    for setting in ("vanilla", "counter_edit"):
        kwargs = dict(common_kwargs)
        if setting == "counter_edit":
            kwargs.update(
                {
                    "swe_touch_scenarios_path": str(scenarios_dir.resolve()),
                    "swe_touch_intervention_mode": "patch_message",
                    "swe_touch_simulator_model": simulator_model,
                }
            )
        config = JobConfig(
            job_name=f"swe-touch__{_safe_name(model)}__{setting}",
            jobs_dir=(output_dir / "jobs").resolve(),
            n_attempts=repetitions,
            n_concurrent_trials=concurrency,
            agent_timeout_multiplier=1.0,
            retry=RetryConfig(max_retries=2),
            agents=[
                AgentConfig(
                    name="mini-swe-agent-external",
                    model_name=model,
                    max_timeout_sec=agent_timeout_sec,
                    kwargs=kwargs,
                )
            ],
            datasets=[
                DatasetConfig(path=tasks_dir.resolve(), task_names=task_names)
            ],
        )
        path = output_dir / f"{setting}.json"
        path.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
        paths[setting] = path
    return paths


def run_job_configs(paths: list[Path]) -> None:
    for path in paths:
        subprocess.run(
            [sys.executable, "-m", "harbor.cli.main", "run", "--config", str(path)],
            check=True,
        )


def _safe_name(value: str) -> str:
    return "".join(
        character if character.isalnum() else "-" for character in value
    ).strip("-")
