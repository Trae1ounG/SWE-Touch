from __future__ import annotations

import json
from pathlib import Path

from harbor.swe_touch.runtime.schemas import CounterEditScenario


def load_scenario(path: Path | str) -> CounterEditScenario:
    scenario_path = Path(path)
    return CounterEditScenario.model_validate_json(scenario_path.read_text())


def load_scenarios(paths: list[Path | str]) -> list[CounterEditScenario]:
    return [load_scenario(path) for path in paths]


def load_scenario_directory(path: Path | str) -> list[CounterEditScenario]:
    root = Path(path)
    if (root / "index.json").exists():
        index = json.loads((root / "index.json").read_text())
        return [load_scenario(root / item["path"]) for item in index["scenarios"]]
    return [load_scenario(path) for path in sorted(root.rglob("*.json"))]
