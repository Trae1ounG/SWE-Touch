from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from harbor.models.trial.result import TrialResult


def aggregate_results(jobs_dir: Path, output: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    occurrences: dict[tuple[str, str, str], int] = defaultdict(int)
    for result_path in sorted(jobs_dir.rglob("result.json")):
        result = TrialResult.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
        model = (
            result.agent_info.model_info.name
            if result.agent_info.model_info
            else "unknown"
        )
        setting = _setting(result)
        instance_id = result.task_id.get_name()
        key = (model, setting, instance_id)
        occurrences[key] += 1
        input_tokens, cache_tokens, output_tokens, cost = (
            result.compute_token_cost_totals()
        )
        rows.append(
            {
                "model": model,
                "setting": setting,
                "repetition": occurrences[key],
                "instance_id": instance_id,
                "resolved": _resolved(result),
                "error": result.exception_info.exception_type
                if result.exception_info
                else "",
                "input_tokens": input_tokens,
                "cache_tokens": cache_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
                "result_path": str(result_path),
            }
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_rows(output, rows)
    summary = _summarize(rows)
    output.with_suffix(".summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"trials": len(rows), "summary": summary, "output": str(output)}


def _setting(result: TrialResult) -> str:
    kwargs = result.config.agent.kwargs
    return "counter_edit" if kwargs.get("swe_touch_scenarios_path") else "vanilla"


def _resolved(result: TrialResult) -> bool | None:
    if result.exception_info is not None or result.verifier_result is None:
        return None
    rewards = result.verifier_result.rewards or {}
    if "reward" in rewards:
        return float(rewards["reward"]) >= 1.0
    if len(rewards) == 1:
        return float(next(iter(rewards.values()))) >= 1.0
    return None


def _summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["model"], row["setting"])].append(row)
    summary = []
    for (model, setting), group in sorted(grouped.items()):
        verified = [row for row in group if row["resolved"] is not None]
        solved = sum(row["resolved"] is True for row in verified)
        summary.append(
            {
                "model": model,
                "setting": setting,
                "completed": len(group),
                "verified": len(verified),
                "errors": len(group) - len(verified),
                "solved": solved,
                "resolve_rate": solved / len(verified) if verified else None,
            }
        )
    return summary


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "model",
        "setting",
        "repetition",
        "instance_id",
        "resolved",
        "error",
        "input_tokens",
        "cache_tokens",
        "output_tokens",
        "cost_usd",
        "result_path",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
