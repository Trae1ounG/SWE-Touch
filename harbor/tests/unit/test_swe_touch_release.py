from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from harbor.agents.external.bridge import (
    SWE_TOUCH_USER_MESSAGES_FIELD,
    EnvironmentBridgeServer,
)
from harbor.agents.external.runners.mini_swe_agent_runner import (
    HarborBridgeEnvironment,
    _build_swe_touch_agent_class,
)
from harbor.agents.external.mini_swe_agent import MiniSweAgentExternal
from harbor.models.job.config import JobConfig
from harbor.models.task.task import Task
from harbor.environments.base import ExecResult
from harbor.swe_touch.gate import collect_gate, prepare_gate
from harbor.swe_touch.jobs import write_paired_job_configs
from harbor.swe_touch.records import materialize_scenarios, read_records
from harbor.swe_touch.runtime.command_events import events_from_shell_command
from harbor.swe_touch.runtime.harness import CounterEditHarness
from harbor.swe_touch.runtime.remote import CounterEditController
from harbor.swe_touch.runtime.schemas import AgentEvent
from harbor.swe_touch.synthesis import TASK_INSTRUCTION_PATH, prepare_synthesis
from harbor.swe_touch.tasks import resolve_task_names
from harbor.swe_touch.runtime.scenario_store import load_scenario_directory
from harbor.swe_touch.runtime.user_simulator import (
    OpenAIResponsesUserSimulatorClient,
    USER_SIMULATOR_PROMPT_SHA256,
    UserSimulator,
    UserSimulatorContext,
    load_counter_edit_user_simulator_prompt,
)


def test_user_simulator_prompt_matches_release_checksum() -> None:
    prompt = load_counter_edit_user_simulator_prompt()
    assert hashlib.sha256(prompt.encode()).hexdigest() == USER_SIMULATOR_PROMPT_SHA256


def test_bridge_keeps_user_message_out_of_tool_output() -> None:
    bridge = object.__new__(EnvironmentBridgeServer)
    bridge._next_agent_command_index = lambda: 1
    bridge._maybe_apply_swe_touch = lambda *args, **kwargs: SimpleNamespace(
        scenario_id="scenario-1",
        message_visible=True,
        message="Please keep my implementation.",
    )
    bridge._swe_touch_scenario_already_recorded = lambda scenario_id: False
    bridge._record_swe_touch_intervention = lambda *args, **kwargs: None
    bridge._record_agent_event = lambda *args, **kwargs: None

    response = {"stdout": "tool output\n", "stderr": "", "return_code": 0}
    result = bridge._apply_swe_touch_intervention(
        {"command": "sed -n '1,20p' source.py"},
        response,
        command_result=dict(response),
    )

    assert result["stdout"] == "tool output\n"
    assert result[SWE_TOUCH_USER_MESSAGES_FIELD] == [
        "Please keep my implementation."
    ]


def test_runner_adds_simulator_output_as_new_user_message() -> None:
    environment = HarborBridgeEnvironment(
        bridge_url="http://bridge.invalid",
        bridge_token="token",
        timeout=30,
    )
    environment._post = lambda path, payload: {
        "stdout": "tool output\n",
        "stderr": "",
        "return_code": 0,
        SWE_TOUCH_USER_MESSAGES_FIELD: ["Please keep my implementation."],
    }
    environment._check_finished = lambda result: None

    class FakeModel:
        @staticmethod
        def format_message(*, role: str, content: str) -> dict[str, str]:
            return {"role": role, "content": content}

    class FakeAgent:
        def __init__(self, env: HarborBridgeEnvironment) -> None:
            self.env = env
            self.model = FakeModel()
            self.messages: list[dict[str, str]] = []

        def add_messages(self, *messages: dict[str, str]) -> list[dict[str, str]]:
            self.messages.extend(messages)
            return list(messages)

        def execute_actions(self, message: dict) -> list[dict[str, str]]:
            result = self.env.execute(message["extra"]["actions"][0])
            return self.add_messages({"role": "tool", "content": result["output"]})

    agent = _build_swe_touch_agent_class(FakeAgent)(environment)
    emitted = agent.execute_actions(
        {"extra": {"actions": [{"command": "sed -n '1,20p' source.py"}]}}
    )

    assert emitted == [
        {"role": "tool", "content": "tool output\n"},
        {"role": "user", "content": "Please keep my implementation."},
    ]
    assert agent.messages == emitted


def test_external_runner_bypasses_proxy_for_local_bridge(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NO_PROXY", "internal.example")
    agent = MiniSweAgentExternal(logs_dir=tmp_path, model_name="example")

    env = agent._build_process_env()

    assert "internal.example" in env["NO_PROXY"].split(",")
    assert "127.0.0.1" in env["NO_PROXY"].split(",")
    assert "localhost" in env["NO_PROXY"].split(",")
    assert env["no_proxy"] == env["NO_PROXY"]


def test_user_simulator_returns_llm_message_when_available() -> None:
    class FixedClient:
        def complete(self, messages: list[dict[str, str]]) -> str:
            assert messages[0]["role"] == "system"
            assert messages[1]["role"] == "user"
            return "I checked the implementation in this file, so please keep that change."

    result = UserSimulator(
        model_name="openai/example-model",
        client=FixedClient(),
    ).generate(
        UserSimulatorContext(
            instance_id="example__task-1",
            intervention_index=1,
            codebase="example",
            task_description="Fix the parser.",
            region_path="source.py",
            start_line=10,
            end_line=12,
            trigger_reason="code_region",
            command="sed -n '10,12p' source.py",
        )
    )

    assert result.message_source == "llm"
    assert result.message_model == "openai/example-model"
    assert result.fallback_reason is None


def test_user_simulator_uses_configured_responses_client(monkeypatch) -> None:
    monkeypatch.setenv(
        "SWE_TOUCH_SIMULATOR_RESPONSES_BASE_URL", "https://example.test/v1"
    )
    monkeypatch.setenv("SWE_TOUCH_SIMULATOR_API_KEY", "test-key")

    client = UserSimulator(model_name="gpt-4o")._default_client()

    assert isinstance(client, OpenAIResponsesUserSimulatorClient)
    assert client.base_url == "https://example.test/v1"


def test_root_level_source_file_commands_emit_region_events() -> None:
    read_events = events_from_shell_command("rg value source.py")
    edit_events = events_from_shell_command("sed -i 's/old/new/' source.py")

    assert [(event.event_type, event.path) for event in read_events] == [
        ("read", "source.py")
    ]
    assert [(event.event_type, event.path) for event in edit_events] == [
        ("edit", "source.py")
    ]


def test_release_record_materializes_three_runtime_scenarios(tmp_path: Path) -> None:
    record_path = tmp_path / "records.jsonl"
    record_path.write_text(json.dumps(_record()) + "\n", encoding="utf-8")

    manifest = materialize_scenarios(read_records(record_path), tmp_path / "scenarios")
    scenarios = load_scenario_directory(tmp_path / "scenarios")

    assert manifest == {"records": 1, "scenarios": 3}
    assert [scenario.user.intervention_index for scenario in scenarios] == [1, 2, 3]
    assert all(scenario.user.role == "counter_edit_user" for scenario in scenarios)
    assert [scenario.trigger.event for scenario in scenarios] == [
        "read_or_edit",
        "edit",
        "edit",
    ]


def test_later_rounds_wait_for_edit_before_reapplying_patch(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "source.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "source.py"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=SWE-Touch",
            "-c",
            "user.email=swe-touch@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=repository,
        check=True,
    )
    record_path = tmp_path / "records.jsonl"
    record_path.write_text(json.dumps(_record()) + "\n", encoding="utf-8")
    scenario_dir = tmp_path / "scenarios"
    materialize_scenarios(read_records(record_path), scenario_dir)
    harness = CounterEditHarness(load_scenario_directory(scenario_dir))
    read = AgentEvent(event_type="read", path="source.py")
    edit = AgentEvent(event_type="edit", path="source.py")

    first = harness.observe(
        read,
        repository=repository,
        scenario_dir=scenario_dir / "example__task-1",
    )
    assert first is not None and first.patch_applied
    assert harness.observe(read, repository=repository) is None

    (repository / "source.py").write_text("value = 1\n", encoding="utf-8")
    second = harness.observe(
        edit,
        repository=repository,
        scenario_dir=scenario_dir / "example__task-1",
    )
    assert second is not None and second.patch_applied
    assert (repository / "source.py").read_text(encoding="utf-8") == "value = 2\n"


async def test_failed_patch_does_not_consume_runtime_scenario(tmp_path: Path) -> None:
    record_path = tmp_path / "records.jsonl"
    record_path.write_text(json.dumps(_record()) + "\n", encoding="utf-8")
    scenario_dir = tmp_path / "scenarios"
    materialize_scenarios(read_records(record_path), scenario_dir)
    scenarios = load_scenario_directory(scenario_dir)
    harness = CounterEditHarness(scenarios)
    controller = CounterEditController(
        scenario_dir=scenario_dir,
        harness=harness,
        current_instance_id="example__task-1",
    )

    class FailedPatchEnvironment:
        def __init__(self) -> None:
            self.commands: list[str] = []

        async def upload_file(self, local_path: Path, remote_path: str) -> None:
            return None

        async def exec(self, *, command: str, **kwargs: object) -> ExecResult:
            self.commands.append(command)
            if command.startswith("mkdir -p .git/swe-touch/interventions"):
                return ExecResult(return_code=0)
            if command.startswith("rm -f .git/swe-touch/interventions"):
                return ExecResult(return_code=0)
            return ExecResult(
                return_code=43,
                stderr="patch application produced no repository state change",
            )

    environment = FailedPatchEnvironment()
    intervention = await controller.maybe_intervene(
        command="sed -n '1,1p' source.py",
        cwd="/testbed",
        environment=environment,  # type: ignore[arg-type]
    )

    assert intervention is None
    assert harness.trigger_count(scenarios[0].scenario_id) == 0
    assert any(
        command.startswith("rm -f .git/swe-touch/interventions")
        for command in environment.commands
    )


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


def test_gate_collection_preserves_candidates_with_missing_results(
    tmp_path: Path,
) -> None:
    manifest = {
        "schema_version": "1.0.0",
        "tasks": [
            {
                "task_name": f"task__{variant}",
                "instance_id": "example__task-1",
                "candidate_id": "candidate-1",
                "variant": variant,
            }
            for variant in (
                "reference_only",
                "user_edit_only",
                "user_edit_plus_reference",
            )
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "gates.json"

    summary = collect_gate(tmp_path / "jobs", manifest_path, output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert summary["candidates"] == 1
    assert summary["accepted"] == 0
    [candidate] = payload["candidates"]
    assert candidate["passes_validation"] is False
    for variant in (
        "reference_only",
        "user_edit_only",
        "user_edit_plus_reference",
    ):
        assert candidate[variant] == {
            "resolved": None,
            "error": "missing_result",
            "result_path": None,
        }


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
                {
                    "order": order,
                    "delivery": "patch_and_message",
                    "patch": patch,
                    "trigger": {
                        "event": "read_or_edit" if order == 1 else "edit",
                        "max_triggers": 1,
                        "regions": patch["target_regions"],
                    },
                }
                for order in range(1, 4)
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
