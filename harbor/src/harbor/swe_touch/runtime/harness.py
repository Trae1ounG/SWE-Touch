from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from harbor.swe_touch.runtime.schemas import AgentEvent, CounterEditScenario


class PatchApplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class UserIntervention:
    scenario_id: str
    instance_id: str
    message: str
    patch_kind: str
    patch_applied: bool
    patch_id: str | None = None
    patch_diff_sha256: str | None = None
    patch_source: str | None = None
    patch_error: str | None = None
    message_visible: bool = True
    intervention_mode: str = "patch_message"
    message_source: str | None = None
    message_prompt_id: str | None = None
    message_prompt_sha256: str | None = None
    message_model: str | None = None
    message_fallback_reason: str | None = None
    simulator_raw_output: str | None = None
    intervention_index: int = 1
    policy_id: str | None = None
    validation_status: str | None = None
    trigger_type: str | None = None
    trigger_event: str | None = None
    trigger_regions: tuple[dict[str, int | str], ...] = ()


class CounterEditHarness:
    """Deterministic user-intervention layer for SWE-style tasks.

    The harness is intentionally independent from any specific agent or evaluator.
    Agent adapters only need to feed normalized AgentEvent objects into observe().
    """

    def __init__(self, scenarios: list[CounterEditScenario]) -> None:
        self._scenarios = list(scenarios)
        self._trigger_counts: dict[str, int] = {}

    @property
    def scenarios(self) -> list[CounterEditScenario]:
        return list(self._scenarios)

    def matching_scenario(
        self,
        event: AgentEvent,
        *,
        blocked_scenario_ids: set[str] | None = None,
        current_instance_id: str | None = None,
    ) -> CounterEditScenario | None:
        blocked_scenario_ids = blocked_scenario_ids or set()
        for scenario in self._scenarios:
            if current_instance_id is not None and not _same_instance_id(
                scenario.instance_id,
                current_instance_id,
            ):
                continue
            if scenario.scenario_id in blocked_scenario_ids:
                continue
            count = self._trigger_counts.get(scenario.scenario_id, 0)
            if count >= scenario.max_triggers:
                continue
            if scenario.trigger.matches(event):
                return scenario
        return None

    def reserve_matching_scenario(
        self,
        event: AgentEvent,
        *,
        blocked_scenario_ids: set[str] | None = None,
        current_instance_id: str | None = None,
    ) -> CounterEditScenario | None:
        scenario = self.matching_scenario(
            event,
            blocked_scenario_ids=blocked_scenario_ids,
            current_instance_id=current_instance_id,
        )
        if scenario is None:
            return None
        count = self._trigger_counts.get(scenario.scenario_id, 0)
        self._trigger_counts[scenario.scenario_id] = count + 1
        return scenario

    def trigger_count(self, scenario_id: str) -> int:
        return self._trigger_counts.get(scenario_id, 0)

    def reserve_scenario(self, scenario: CounterEditScenario) -> bool:
        count = self._trigger_counts.get(scenario.scenario_id, 0)
        if count >= scenario.max_triggers:
            return False
        self._trigger_counts[scenario.scenario_id] = count + 1
        return True

    def build_intervention(
        self,
        scenario: CounterEditScenario,
        *,
        patch_applied: bool,
        patch_error: str | None = None,
        message: str | None = None,
        message_visible: bool | None = None,
        intervention_mode: str = "patch_message",
        message_source: str | None = None,
        message_prompt_id: str | None = None,
        message_prompt_sha256: str | None = None,
        message_model: str | None = None,
        message_fallback_reason: str | None = None,
        simulator_raw_output: str | None = None,
        intervention_index: int | None = None,
        patch_diff_sha256: str | None = None,
    ) -> UserIntervention:
        return UserIntervention(
            scenario_id=scenario.scenario_id,
            instance_id=scenario.instance_id,
            message=scenario.user.message if message is None else message,
            patch_kind=scenario.patch.patch_kind if scenario.patch else "none",
            patch_id=scenario.patch.patch_id if scenario.patch else None,
            patch_diff_sha256=patch_diff_sha256,
            patch_source=scenario.patch.patch_source if scenario.patch else None,
            patch_applied=patch_applied,
            patch_error=patch_error,
            message_visible=patch_applied
            if message_visible is None
            else message_visible,
            intervention_mode=intervention_mode,
            message_source=message_source
            if message_source is not None
            else scenario.user.message_source,
            message_prompt_id=(
                message_prompt_id
                if message_prompt_id is not None
                else scenario.user.message_prompt_id
            ),
            message_prompt_sha256=message_prompt_sha256,
            message_model=message_model
            if message_model is not None
            else scenario.user.message_model,
            message_fallback_reason=message_fallback_reason,
            simulator_raw_output=simulator_raw_output,
            intervention_index=(
                intervention_index
                if intervention_index is not None
                else scenario.user.intervention_index
            ),
            policy_id=scenario.policy.policy_id if scenario.policy else None,
            validation_status=scenario.policy.validation_status
            if scenario.policy
            else None,
            trigger_type=getattr(scenario.trigger, "type", None),
            trigger_event=getattr(scenario.trigger, "event", None),
            trigger_regions=_trigger_regions_summary(scenario),
        )

    def mark_triggered(
        self,
        scenario: CounterEditScenario,
        *,
        patch_applied: bool,
        patch_error: str | None = None,
        message_visible: bool | None = None,
    ) -> UserIntervention:
        count = self._trigger_counts.get(scenario.scenario_id, 0)
        self._trigger_counts[scenario.scenario_id] = count + 1
        return self.build_intervention(
            scenario,
            patch_applied=patch_applied,
            patch_error=patch_error,
            message_visible=message_visible,
        )

    def observe(
        self,
        event: AgentEvent,
        *,
        repository: Path | None = None,
        scenario_dir: Path | None = None,
    ) -> UserIntervention | None:
        scenario = self.matching_scenario(event)
        if scenario is None:
            return None
        patch_applied = False
        if repository is not None and scenario.patch is not None:
            diff = scenario.load_patch_diff(scenario_dir)
            apply_patch(repository=repository, diff=diff)
            patch_applied = True
        return self.mark_triggered(
            scenario,
            patch_applied=patch_applied,
            message_visible=scenario.patch is None or patch_applied,
        )


def _trigger_regions_summary(
    scenario: CounterEditScenario,
) -> tuple[dict[str, int | str], ...]:
    trigger = scenario.trigger
    regions = getattr(trigger, "regions", None)
    if regions is None:
        regions = [trigger] if getattr(trigger, "type", None) == "code_region" else []
    summary: list[dict[str, int | str]] = []
    for region in regions:
        path = getattr(region, "path", None)
        start_line = getattr(region, "start_line", None)
        end_line = getattr(region, "end_line", None)
        event = getattr(region, "event", None)
        if path is None or start_line is None or end_line is None:
            continue
        row: dict[str, int | str] = {
            "path": path,
            "start_line": int(start_line),
            "end_line": int(end_line),
        }
        if event is not None:
            row["event"] = str(event)
        summary.append(row)
    return tuple(summary)


def _same_instance_id(left: str, right: str) -> bool:
    left_alias = _instance_id_alias(left)
    right_alias = _instance_id_alias(right)
    return (
        left_alias == right_alias
        or left_alias.startswith(f"{right_alias}__")
        or right_alias.startswith(f"{left_alias}__")
    )


def _instance_id_alias(instance_id: str) -> str:
    for prefix in ("swebenchpro_", "deepswe_"):
        if instance_id.startswith(prefix):
            return instance_id[len(prefix) :]
    return instance_id


def apply_patch(*, repository: Path, diff: str) -> None:
    result = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        input=diff,
        text=True,
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        output = result.stderr or result.stdout or "no output"
        raise PatchApplyError(output.strip())
