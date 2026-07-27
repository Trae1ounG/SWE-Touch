from __future__ import annotations

import hashlib
import logging
import shlex
import tempfile
from asyncio import Lock
from collections.abc import Callable, Mapping
from pathlib import Path
import re
from typing import Any
from typing import Literal

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.swe_touch.runtime.command_events import events_from_shell_command
from harbor.swe_touch.runtime.critical_regions import (
    CommandRecord,
    events_from_command_record,
)
from harbor.swe_touch.runtime.harness import (
    PatchApplyError,
    CounterEditHarness,
    UserIntervention,
)
from harbor.swe_touch.runtime.scenario_store import load_scenario_directory
from harbor.swe_touch.runtime.schemas import (
    AgentEvent,
    CodeRegionTrigger,
    CodeRegionsTrigger,
    RegionStateChangeTrigger,
    ScheduledCommandTrigger,
    CounterEditScenario,
)
from harbor.swe_touch.runtime.user_simulator import UserSimulator, UserSimulatorContext

InterventionMode = Literal["patch_message", "text_only", "patch_only"]
logger = logging.getLogger(__name__)


class CounterEditController:
    """Apply SWE-Touch scenarios inside a Harbor environment."""

    def __init__(
        self,
        *,
        scenario_dir: Path,
        harness: CounterEditHarness,
        intervention_mode: InterventionMode = "patch_message",
        current_instance_id: str | None = None,
        user_simulator: UserSimulator | None = None,
        task_description: str = "",
    ) -> None:
        self.scenario_dir = scenario_dir
        self.harness = harness
        self.intervention_mode = intervention_mode
        self.current_instance_id = current_instance_id
        self.user_simulator = user_simulator
        self.task_description = task_description
        self._intervention_lock = Lock()
        self._intervention_count = 0

    @classmethod
    def from_directory(
        cls,
        scenario_dir: Path | str,
        *,
        intervention_mode: InterventionMode = "patch_message",
        current_instance_id: str | None = None,
        user_simulator_model: str | None = None,
        user_simulator_get_env: Callable[[str], str | None] | None = None,
        user_simulator_timeout_sec: int = 60,
        user_simulator_max_tokens: int = 512,
        task_description: str = "",
    ) -> "CounterEditController":
        root = Path(scenario_dir)
        user_simulator = (
            UserSimulator(
                model_name=user_simulator_model,
                get_env=user_simulator_get_env,
                timeout_sec=user_simulator_timeout_sec,
                max_tokens=user_simulator_max_tokens,
            )
            if user_simulator_model
            else None
        )
        return cls(
            scenario_dir=root,
            harness=CounterEditHarness(load_scenario_directory(root)),
            intervention_mode=intervention_mode,
            current_instance_id=current_instance_id,
            user_simulator=user_simulator,
            task_description=task_description,
        )

    async def maybe_intervene(
        self,
        *,
        command: str,
        cwd: str | None,
        environment: BaseEnvironment,
        blocked_scenario_ids: set[str] | None = None,
        command_result: Mapping[str, Any] | None = None,
        recent_events: tuple[Mapping[str, Any], ...] = (),
        command_index: int | None = None,
    ) -> UserIntervention | None:
        for event in _events_from_command_and_result(
            command,
            command_result,
            command_index=command_index,
        ):
            async with self._intervention_lock:
                scenario = self.harness.reserve_matching_scenario(
                    event,
                    blocked_scenario_ids=blocked_scenario_ids,
                    current_instance_id=self.current_instance_id,
                )
                if scenario is None:
                    continue
            reserved = await self._reserve_remote_scenario(
                scenario_id=scenario.scenario_id,
                cwd=_repo_cwd(cwd, command, self.current_instance_id),
                environment=environment,
            )
            if not reserved:
                self.harness.release_scenario(scenario)
                return None
            patch_error = None
            patch_applied = False
            patch_diff_sha256 = None
            if self.intervention_mode != "text_only" and scenario.patch is not None:
                diff = scenario.load_patch_diff(
                    self.scenario_dir / scenario.instance_id
                )
                patch_diff_sha256 = hashlib.sha256(diff.encode()).hexdigest()
                try:
                    await self._apply_remote_patch(
                        diff=diff,
                        cwd=_repo_cwd(cwd, command, self.current_instance_id),
                        environment=environment,
                    )
                    patch_applied = True
                except PatchApplyError as exc:
                    patch_error = str(exc)
            if patch_error is not None:
                self.harness.release_scenario(scenario)
                await self._release_remote_scenario(
                    scenario_id=scenario.scenario_id,
                    cwd=_repo_cwd(cwd, command, self.current_instance_id),
                    environment=environment,
                )
                logger.warning(
                    "SWE-Touch patch was not applied for %s; waiting for the next matching edit: %s",
                    scenario.scenario_id,
                    patch_error,
                )
                return None
            message_visible = self.intervention_mode == "text_only" or (
                self.intervention_mode == "patch_message"
                and (scenario.patch is None or patch_applied)
            )
            intervention_index = self._reserve_intervention_index()
            message = scenario.user.message if message_visible else ""
            simulator_result = None
            if message_visible and self.user_simulator is not None:
                simulator_result = self.user_simulator.generate(
                    self._user_simulator_context(
                        scenario=scenario,
                        intervention_index=intervention_index,
                        command=command,
                        command_result=command_result or {},
                        recent_events=recent_events,
                    )
                )
                message = simulator_result.message
            return self.harness.build_intervention(
                scenario,
                patch_applied=patch_applied,
                patch_error=patch_error,
                message=message,
                message_visible=message_visible,
                intervention_mode=self.intervention_mode,
                message_source=simulator_result.message_source
                if simulator_result
                else None,
                message_prompt_id=(
                    simulator_result.message_prompt_id if simulator_result else None
                ),
                message_prompt_sha256=(
                    simulator_result.message_prompt_sha256 if simulator_result else None
                ),
                message_model=simulator_result.message_model
                if simulator_result
                else None,
                message_fallback_reason=(
                    simulator_result.fallback_reason if simulator_result else None
                ),
                simulator_raw_output=simulator_result.raw_output
                if simulator_result
                else None,
                intervention_index=intervention_index,
                patch_diff_sha256=patch_diff_sha256,
            )
        return None

    def _reserve_intervention_index(self) -> int:
        self._intervention_count += 1
        return self._intervention_count

    def _user_simulator_context(
        self,
        *,
        scenario: CounterEditScenario,
        intervention_index: int,
        command: str,
        command_result: Mapping[str, Any],
        recent_events: tuple[Mapping[str, Any], ...] = (),
    ) -> UserSimulatorContext:
        path, start_line, end_line = _scenario_region(scenario)
        user_edit = (
            scenario.load_patch_diff(self.scenario_dir / scenario.instance_id)
            if scenario.patch is not None
            else ""
        )
        return UserSimulatorContext(
            instance_id=scenario.instance_id,
            intervention_index=max(1, intervention_index),
            codebase=scenario.instance_id.split("__", 1)[0],
            task_description=self.task_description,
            region_path=path,
            start_line=start_line,
            end_line=end_line,
            trigger_reason=scenario.trigger.type,
            command=command,
            command_result=command_result,
            seed_message=scenario.user.message,
            user_edit=user_edit,
            recent_events=recent_events,
        )

    async def _reserve_remote_scenario(
        self,
        *,
        scenario_id: str,
        cwd: str,
        environment: BaseEnvironment,
    ) -> bool:
        sentinel_name = re.sub(r"[^A-Za-z0-9_.-]", "_", scenario_id)
        sentinel_path = f".git/swe-touch/interventions/{sentinel_name}"
        result = await environment.exec(
            command=(
                "mkdir -p .git/swe-touch/interventions && "
                f"if ( set -C; : > {shlex.quote(sentinel_path)} ) 2>/dev/null; "
                "then exit 0; else exit 42; fi"
            ),
            cwd=cwd,
            timeout_sec=10,
        )
        return result.return_code == 0

    async def _release_remote_scenario(
        self,
        *,
        scenario_id: str,
        cwd: str,
        environment: BaseEnvironment,
    ) -> None:
        sentinel_name = re.sub(r"[^A-Za-z0-9_.-]", "_", scenario_id)
        sentinel_path = f".git/swe-touch/interventions/{sentinel_name}"
        await environment.exec(
            command=f"rm -f {shlex.quote(sentinel_path)}",
            cwd=cwd,
            timeout_sec=10,
        )

    async def _apply_remote_patch(
        self,
        *,
        diff: str,
        cwd: str,
        environment: BaseEnvironment,
    ) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False) as local:
            local.write(diff)
            local_path = Path(local.name)
        remote_path = f"/tmp/harbor-swe-user-{local_path.name}"
        try:
            await environment.upload_file(local_path, remote_path)
            quoted_remote_path = shlex.quote(remote_path)
            result = await environment.exec(
                command=(
                    "state_digest() { "
                    "{ git diff --binary -- .; "
                    "git ls-files --others --exclude-standard -z "
                    "| sort -z | xargs -0 -r sha256sum; } "
                    "| sha256sum | cut -d' ' -f1; }; "
                    "before=$(state_digest); "
                    f"(git apply --whitespace=nowarn {quoted_remote_path} "
                    f"|| patch --forward --fuzz=5 --batch -p1 -i {quoted_remote_path}); "
                    "rc=$?; after=$(state_digest); "
                    "if [ $rc -ne 0 ]; then exit $rc; fi; "
                    'if [ "$before" = "$after" ]; then '
                    "echo 'patch application produced no repository state change' >&2; "
                    "exit 43; fi"
                ),
                cwd=cwd,
                timeout_sec=60,
            )
            if result.return_code != 0:
                result = await self._apply_remote_patch_by_context(
                    diff_path=remote_path,
                    cwd=cwd,
                    environment=environment,
                )
        finally:
            local_path.unlink(missing_ok=True)
        if result.return_code != 0:
            output = result.stderr or result.stdout or "no output"
            raise PatchApplyError(output.strip())

    async def _apply_remote_patch_by_context(
        self,
        *,
        diff_path: str,
        cwd: str,
        environment: BaseEnvironment,
    ) -> ExecResult:
        helper_path = Path(__file__).with_name("patch_fallback.py")
        remote_helper_path = f"/tmp/harbor-swe-user-{helper_path.name}"
        await environment.upload_file(helper_path, remote_helper_path)
        return await environment.exec(
            command=(
                "state_digest() { "
                "{ git diff --binary -- .; "
                "git ls-files --others --exclude-standard -z "
                "| sort -z | xargs -0 -r sha256sum; } "
                "| sha256sum | cut -d' ' -f1; }; "
                "before=$(state_digest); "
                f"python {shlex.quote(remote_helper_path)} "
                f"{shlex.quote(diff_path)} --repository .; rc=$?; "
                "after=$(state_digest); "
                "if [ $rc -ne 0 ]; then exit $rc; fi; "
                'if [ "$before" = "$after" ]; then '
                "echo 'context fallback produced no repository state change' >&2; "
                "exit 43; fi"
            ),
            cwd=cwd,
            timeout_sec=60,
        )


def _scenario_region(scenario: CounterEditScenario) -> tuple[str, int, int]:
    trigger = scenario.trigger
    if isinstance(trigger, CodeRegionTrigger):
        return trigger.path, trigger.start_line, trigger.end_line
    if isinstance(trigger, CodeRegionsTrigger):
        region = trigger.regions[0]
        return region.path, region.start_line, region.end_line
    if isinstance(trigger, RegionStateChangeTrigger):
        return trigger.path, trigger.start_line, trigger.end_line
    if isinstance(trigger, ScheduledCommandTrigger) and trigger.regions:
        region = trigger.regions[0]
        return region.path, region.start_line, region.end_line
    return "unknown", 1, 1


def _events_from_command_and_result(
    command: str,
    command_result: Mapping[str, Any] | None,
    *,
    command_index: int | None = None,
) -> list[AgentEvent]:
    events = list(events_from_shell_command(command))
    if command_index is not None:
        events.append(
            AgentEvent(
                event_type="manual",
                path="",
                command=command,
                command_index=command_index,
            )
        )
    output = _command_output_text(command_result)
    if not output:
        return events
    record = CommandRecord(label="live", step=0, command=command, output=output)
    for event in events_from_command_record(record):
        events.append(
            AgentEvent(
                event_type=event.event_type,
                path=event.path,
                start_line=event.start_line,
                end_line=event.end_line,
                command=command,
            )
        )
    return events


def _command_output_text(command_result: Mapping[str, Any] | None) -> str:
    if not command_result:
        return ""
    parts: list[str] = []
    for key in ("stdout", "stderr"):
        value = command_result.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def _repo_cwd(cwd: str | None, command: str, instance_id: str | None = None) -> str:
    if cwd:
        return cwd
    if "/app" in command:
        return "/app"
    if "/testbed" in command:
        return "/testbed"
    if instance_id and (
        instance_id.startswith("deepswe_")
        or instance_id.startswith("swebenchpro_")
        or instance_id.startswith("deepswe-")
        or instance_id.startswith("swebenchpro-")
    ):
        return "/app"
    return "/testbed"
