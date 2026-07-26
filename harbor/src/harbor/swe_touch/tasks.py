from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def task_index(tasks_dir: Path) -> dict[str, Path]:
    if not tasks_dir.is_dir():
        raise FileNotFoundError(f"Harbor task directory not found: {tasks_dir}")
    return {
        path.name: path
        for path in tasks_dir.iterdir()
        if path.is_dir() and (path / "task.toml").is_file()
    }


def resolve_task(index: dict[str, Path], instance_id: str) -> Path:
    matches = {index[alias] for alias in _instance_id_aliases(instance_id) if alias in index}
    if not matches:
        aliases = ", ".join(_instance_id_aliases(instance_id))
        raise FileNotFoundError(
            f"Harbor task not found for {instance_id!r}; tried directory names: {aliases}"
        )
    if len(matches) > 1:
        paths = ", ".join(sorted(path.name for path in matches))
        raise ValueError(f"Multiple Harbor tasks match {instance_id!r}: {paths}")
    return next(iter(matches))


def resolve_task_names(tasks_dir: Path, instance_ids: Iterable[str]) -> list[str]:
    index = task_index(tasks_dir)
    names = [resolve_task(index, instance_id).name for instance_id in instance_ids]
    if len(names) != len(set(names)):
        raise ValueError("Multiple release records resolve to the same Harbor task")
    return names


def _instance_id_aliases(instance_id: str) -> tuple[str, ...]:
    aliases = [instance_id]
    for prefix in ("swebenchpro_", "deepswe_"):
        if instance_id.startswith(prefix):
            aliases.append(instance_id.removeprefix(prefix))
    aliases.extend(alias.replace("__", "_") for alias in list(aliases))
    return tuple(dict.fromkeys(aliases))
