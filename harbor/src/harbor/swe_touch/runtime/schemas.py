from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

EventType = Literal["read", "edit", "read_or_edit", "manual"]
PatchKind = Literal["counter_edit"]


def _normalize_repo_path(path: str) -> str:
    normalized = path.strip()
    if normalized.startswith("/testbed/"):
        normalized = normalized[len("/testbed/") :]
    return normalized.lstrip("./")


class AgentEvent(BaseModel):
    """A normalized code interaction emitted by an agent adapter."""

    event_type: EventType
    path: str
    start_line: int | None = None
    end_line: int | None = None
    command: str | None = None
    command_index: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def normalize(self) -> "AgentEvent":
        self.path = _normalize_repo_path(self.path)
        if self.start_line is not None and self.end_line is None:
            self.end_line = self.start_line
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class CodeRegionTrigger(BaseModel):
    """Trigger when an agent reads or edits a predefined code region."""

    type: Literal["code_region"] = "code_region"
    path: str
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    event: EventType = "read_or_edit"
    min_overlap_lines: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def normalize(self) -> "CodeRegionTrigger":
        self.path = _normalize_repo_path(self.path)
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self

    def matches(self, event: AgentEvent) -> bool:
        if self.path != event.path:
            return False
        if self.event == "read_or_edit":
            if event.event_type not in {"read", "edit", "read_or_edit"}:
                return False
        elif event.event_type != self.event:
            return False
        if event.start_line is None or event.end_line is None:
            return True
        overlap_start = max(self.start_line, event.start_line)
        overlap_end = min(self.end_line, event.end_line)
        overlap = overlap_end - overlap_start + 1
        return overlap >= self.min_overlap_lines


class CodeRegionsTrigger(BaseModel):
    """Trigger when an agent reads or edits any one of several code regions."""

    type: Literal["code_regions"] = "code_regions"
    regions: list[CodeRegionTrigger] = Field(min_length=1)
    event: EventType = "read_or_edit"
    min_overlap_lines: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def normalize(self) -> "CodeRegionsTrigger":
        for region in self.regions:
            region.event = self.event
            region.min_overlap_lines = self.min_overlap_lines
        return self

    def matches(self, event: AgentEvent) -> bool:
        return any(region.matches(event) for region in self.regions)


class ScheduledCommandTrigger(BaseModel):
    """Trigger after the Nth shell command observed by the remote bridge."""

    type: Literal["scheduled_command_count"] = "scheduled_command_count"
    command_count: int = Field(ge=1)
    regions: list[CodeRegionTrigger] | None = None

    def matches(self, event: AgentEvent) -> bool:
        return (
            event.event_type == "manual"
            and (event.command_index or 0) >= self.command_count
        )


class RegionStateChangeTrigger(BaseModel):
    """Trigger when a previously user-patched region no longer matches its snapshot."""

    type: Literal["region_state_changed"] = "region_state_changed"
    path: str
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    after_scenario_id: str
    min_overlap_lines: int = Field(default=1, gt=0)
    regions: list[CodeRegionTrigger] | None = None

    @model_validator(mode="after")
    def normalize(self) -> "RegionStateChangeTrigger":
        self.path = _normalize_repo_path(self.path)
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        if self.regions:
            for region in self.regions:
                region.event = "edit"
                region.min_overlap_lines = self.min_overlap_lines
        return self

    def matches(self, event: AgentEvent) -> bool:
        if event.event_type not in {"edit", "read_or_edit"}:
            return False
        if self.regions:
            return any(region.matches(event) for region in self.regions)
        if self.path != event.path:
            return False
        if event.start_line is None or event.end_line is None:
            return True
        overlap_start = max(self.start_line, event.start_line)
        overlap_end = min(self.end_line, event.end_line)
        overlap = overlap_end - overlap_start + 1
        return overlap >= self.min_overlap_lines


ScenarioTrigger = Annotated[
    CodeRegionTrigger
    | CodeRegionsTrigger
    | RegionStateChangeTrigger
    | ScheduledCommandTrigger,
    Field(discriminator="type"),
]


class UserPatch(BaseModel):
    patch_id: str
    patch_kind: PatchKind
    patch_source: str | None = None
    diff_path: str | None = None
    diff: str | None = None

    @model_validator(mode="after")
    def has_diff_source(self) -> "UserPatch":
        if not self.diff and not self.diff_path:
            raise ValueError("UserPatch requires either diff or diff_path")
        return self

    def load_diff(self, base_dir: Path | None = None) -> str:
        if self.diff is not None:
            return self.diff
        if self.diff_path is None:
            raise ValueError("UserPatch has no diff source")
        path = Path(self.diff_path)
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        return path.read_text()


class UserSpec(BaseModel):
    role: Literal["counter_edit_user"] = "counter_edit_user"
    message: str
    message_source: str | None = None
    message_prompt_id: Literal["counter_edit_user_simulator"] = (
        "counter_edit_user_simulator"
    )
    message_model: str | None = None
    intervention_index: int = Field(ge=1)


class UserPolicy(BaseModel):
    policy_id: Literal["counter_edit"] = "counter_edit"
    trigger_strategy: str
    validation_status: str | None = None


class CounterEditScenario(BaseModel):
    scenario_id: str
    instance_id: str
    trigger: ScenarioTrigger
    patch: UserPatch | None = None
    user: UserSpec
    policy: UserPolicy | None = None
    max_triggers: int = Field(default=1, ge=1)

    def load_patch_diff(self, scenario_dir: Path | None = None) -> str:
        if self.patch is None:
            return ""
        return self.patch.load_diff(scenario_dir)
