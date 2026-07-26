from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.agents.external.bridge import EnvironmentBridgeServer
from harbor.agents.external.uv_runner import UvHarnessRunner
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.agents.installed.mini_swe_agent import (
    _message_usage,
    convert_and_save_trajectory,
)
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.swe_touch.runtime.remote import CounterEditController


_SWE_TOUCH_EVAL_SUPPORT_NAMES = (
    "parser.py",
    "run_script.sh",
    "test.sh",
    "test.patch",
    "gold.patch",
    "patch_fallback.py",
)


def swe_touch_eval_support_files(script_path: Path) -> list[Path]:
    tests_dir = script_path.parent
    return [
        tests_dir / name
        for name in _SWE_TOUCH_EVAL_SUPPORT_NAMES
        if (tests_dir / name).is_file()
    ]


class MiniSweAgentExternal(BaseAgent):
    """Run mini-swe-agent on the control side against a Harbor environment."""

    SUPPORTS_ATIF = True
    _RUNNER_MODULE = "harbor.agents.external.runners.mini_swe_agent_runner"
    _TRAJECTORY_FILENAME = "mini-swe-agent.trajectory.json"

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        version: str | None = "2.4.1",
        python: str | None = None,
        config_specs: list[str] | None = None,
        model_class: str | None = None,
        cost_limit: float | None = None,
        step_limit: int | None = None,
        command_timeout_sec: int = 30,
        model_request_timeout_sec: int | None = None,
        model_max_tokens: int | None = None,
        model_max_retries: int | None = None,
        model_retry_initial_sleep_sec: float | None = None,
        model_retry_max_sleep_sec: float | None = None,
        bridge_max_retries: int | None = None,
        bridge_retry_sleep_sec: float | None = None,
        swe_touch_scenarios_path: str | None = None,
        swe_touch_log_path: str | None = None,
        swe_touch_intervention_mode: str = "patch_message",
        swe_touch_simulator_model: str | None = None,
        swe_touch_simulator_timeout_sec: int = 60,
        swe_touch_simulator_max_tokens: int = 220,
        swe_touch_eval_script_path: str | None = None,
        swe_touch_eval_config_path: str | None = None,
        swe_touch_auto_upload_eval: bool = False,
        task_dir: Path | None = None,
        runner: UvHarnessRunner | None = None,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            logs_dir=logs_dir,
            model_name=model_name,
            **kwargs,
        )
        self._version = version
        self._python = python
        self._config_specs = config_specs or ["mini"]
        self._model_class = model_class
        self._cost_limit = cost_limit
        self._step_limit = step_limit
        self._command_timeout_sec = command_timeout_sec
        self._model_request_timeout_sec = model_request_timeout_sec
        self._model_max_tokens = model_max_tokens
        self._model_max_retries = model_max_retries
        self._model_retry_initial_sleep_sec = model_retry_initial_sleep_sec
        self._model_retry_max_sleep_sec = model_retry_max_sleep_sec
        self._bridge_max_retries = bridge_max_retries
        self._bridge_retry_sleep_sec = bridge_retry_sleep_sec
        self._swe_touch_scenarios_path = swe_touch_scenarios_path
        self._swe_touch_log_path = swe_touch_log_path
        self._swe_touch_intervention_mode = swe_touch_intervention_mode
        self._swe_touch_simulator_model = swe_touch_simulator_model
        self._swe_touch_simulator_timeout_sec = swe_touch_simulator_timeout_sec
        self._swe_touch_simulator_max_tokens = swe_touch_simulator_max_tokens
        self._swe_touch_eval_script_path = swe_touch_eval_script_path
        self._swe_touch_eval_config_path = swe_touch_eval_config_path
        self._swe_touch_auto_upload_eval = swe_touch_auto_upload_eval
        self._task_dir = task_dir
        self._runner = runner or UvHarnessRunner()
        self._extra_env = dict(extra_env or {})

    @staticmethod
    def name() -> str:
        return "mini-swe-agent-external"

    def version(self) -> str | None:
        return self._version

    async def setup(self, environment: BaseEnvironment) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self.model_name:
            raise ValueError("model_name is required for mini-swe-agent-external")

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        instruction_path = self.logs_dir / "instruction.txt"
        output_path = self.logs_dir / self._TRAJECTORY_FILENAME
        instruction_path.write_text(instruction)

        env = self._build_process_env()
        template_vars = await self._remote_template_vars(environment)
        bridge = EnvironmentBridgeServer(
            environment=environment,
            template_vars=template_vars,
            swe_touch_controller=self._swe_touch_controller(context),
            swe_touch_log_path=self._resolved_swe_touch_log_path(),
        )

        await self._upload_swe_touch_eval_files(environment)
        await bridge.start()
        try:
            return_code = await self._runner.run_module(
                module=self._RUNNER_MODULE,
                args=self._runner_args(
                    bridge_url=bridge.url,
                    bridge_token=bridge.token,
                    instruction_path=instruction_path,
                    output_path=output_path,
                ),
                packages=self._packages(),
                logs_dir=self.logs_dir,
                env=env,
                python=self._python,
            )
        finally:
            await bridge.stop()
            await self._upload_agent_logs(environment)

        if return_code != 0:
            raise NonZeroAgentExitCodeError(
                f"mini-swe-agent-external exited with code {return_code}"
            )

    async def _upload_swe_touch_eval_files(self, environment: BaseEnvironment) -> None:
        if (
            not self._swe_touch_eval_script_path
            and not self._swe_touch_eval_config_path
            and not self._swe_touch_auto_upload_eval
        ):
            return

        await environment.exec("mkdir -p /tests /logs/verifier")
        if self._swe_touch_auto_upload_eval:
            if self._task_dir is None:
                raise ValueError("swe_touch_auto_upload_eval requires a local task_dir")
            tests_dir = self._task_dir / "tests"
            if not tests_dir.is_dir():
                raise FileNotFoundError(f"task tests directory not found: {tests_dir}")
            await environment.upload_dir(tests_dir, "/tests")
            test_script = tests_dir / "test.sh"
            if test_script.exists():
                await environment.exec(
                    "cp /tests/test.sh /tests/swe_touch_eval.sh && "
                    "chmod +x /tests/swe_touch_eval.sh"
                )
        if self._swe_touch_eval_script_path:
            script_path = Path(self._swe_touch_eval_script_path)
            await environment.upload_file(
                script_path,
                "/tests/swe_touch_eval.sh",
            )
            await environment.exec("chmod +x /tests/swe_touch_eval.sh")
            for support_path in swe_touch_eval_support_files(script_path):
                remote_path = f"/tests/{support_path.name}"
                await environment.upload_file(support_path, remote_path)
                if support_path.suffix == ".sh":
                    await environment.exec(f"chmod +x {remote_path}")
        if self._swe_touch_eval_config_path:
            await environment.upload_file(
                self._swe_touch_eval_config_path,
                "/tests/config.json",
            )

    def populate_context_post_run(self, context: AgentContext) -> None:
        trajectory_path = self.logs_dir / self._TRAJECTORY_FILENAME
        if not trajectory_path.exists():
            self.logger.debug("mini-swe-agent external trajectory file not found")
            return

        try:
            trajectory = json.loads(trajectory_path.read_text())
        except Exception as exc:
            self.logger.debug("Failed to load mini-swe-agent trajectory: %s", exc)
            return

        n_input_tokens = 0
        n_output_tokens = 0
        n_cache_tokens = 0
        total_cost = ((trajectory.get("info") or {}).get("model_stats") or {}).get(
            "instance_cost"
        ) or 0
        for message in trajectory.get("messages") or []:
            usage = _message_usage(message)
            n_cache_tokens += usage["prompt_tokens_details"].get("cached_tokens") or 0
            n_input_tokens += usage["prompt_tokens"]
            n_output_tokens += usage["completion_tokens"]

        context.n_input_tokens = n_input_tokens
        context.n_output_tokens = n_output_tokens
        context.n_cache_tokens = n_cache_tokens
        context.cost_usd = total_cost

        try:
            convert_and_save_trajectory(
                mini_swe_agent_trajectory_path=trajectory_path,
                atif_trajectory_path=self.logs_dir / "trajectory.json",
                session_id="mini-swe-agent-external",
            )
        except Exception as exc:
            self.logger.debug("Failed to convert mini-swe-agent trajectory: %s", exc)

    def _packages(self) -> list[str]:
        if self._version:
            return [f"mini-swe-agent=={self._version}"]
        return ["mini-swe-agent"]

    def _runner_args(
        self,
        *,
        bridge_url: str,
        bridge_token: str,
        instruction_path: Path,
        output_path: Path,
    ) -> list[str]:
        args = [
            f"--bridge-url={bridge_url}",
            f"--bridge-token={bridge_token}",
            "--task-file",
            str(instruction_path),
            "--output-path",
            str(output_path),
            "--model",
            self._agent_model_name(),
            "--command-timeout-sec",
            str(self._command_timeout_sec),
        ]
        for config_spec in self._config_specs:
            args.extend(["--config", config_spec])
        if model_class := self._runner_model_class():
            args.extend(["--model-class", model_class])
        if self._cost_limit is not None:
            args.extend(["--cost-limit", str(self._cost_limit)])
        if self._step_limit is not None:
            args.extend(["--step-limit", str(self._step_limit)])
        if self._model_request_timeout_sec is not None:
            args.extend(
                ["--model-request-timeout-sec", str(self._model_request_timeout_sec)]
            )
        if self._model_max_tokens is not None:
            args.extend(["--model-max-tokens", str(self._model_max_tokens)])
        if self._model_max_retries is not None:
            args.extend(["--model-max-retries", str(self._model_max_retries)])
        if self._model_retry_initial_sleep_sec is not None:
            args.extend(
                [
                    "--model-retry-initial-sleep-sec",
                    str(self._model_retry_initial_sleep_sec),
                ]
            )
        if self._model_retry_max_sleep_sec is not None:
            args.extend(
                [
                    "--model-retry-max-sleep-sec",
                    str(self._model_retry_max_sleep_sec),
                ]
            )
        if self._bridge_max_retries is not None:
            args.extend(["--bridge-max-retries", str(self._bridge_max_retries)])
        if self._bridge_retry_sleep_sec is not None:
            args.extend(["--bridge-retry-sleep-sec", str(self._bridge_retry_sleep_sec)])
        return args

    def _agent_model_name(self) -> str:
        return str(self.model_name)

    def _runner_model_class(self) -> str | None:
        return self._model_class

    def _build_process_env(self) -> dict[str, str]:
        env = {
            "MSWEA_CONFIGURED": "true",
            "MSWEA_COST_TRACKING": "ignore_errors",
            "MSWEA_SILENT_STARTUP": "1",
        }
        for key in [
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "AZURE_API_BASE",
            "AZURE_API_KEY",
            "AZURE_API_VERSION",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_API_BASE",
            "OPENAI_BASE_URL",
            "MSWEA_API_KEY",
        ]:
            value = self._get_env(key)
            if value is not None:
                env[key] = value
        env.update(self._extra_env)
        return env

    def _swe_touch_controller(
        self, context: AgentContext
    ) -> CounterEditController | None:
        if not self._swe_touch_scenarios_path:
            return None
        return CounterEditController.from_directory(
            Path(self._swe_touch_scenarios_path),
            intervention_mode=self._swe_touch_intervention_mode,  # type: ignore[arg-type]
            current_instance_id=self._current_swe_touch_instance_id(context),
            user_simulator_model=self._swe_touch_simulator_model,
            user_simulator_get_env=self._get_env,
            user_simulator_timeout_sec=self._swe_touch_simulator_timeout_sec,
            user_simulator_max_tokens=self._swe_touch_simulator_max_tokens,
            task_description=(self.logs_dir / "instruction.txt").read_text()
            if (self.logs_dir / "instruction.txt").exists()
            else "",
        )

    def _current_swe_touch_instance_id(self, context: AgentContext) -> str | None:
        metadata = context.metadata or {}
        instance_id = metadata.get("instance_id") or metadata.get("task_id")
        scenario_instance_id = self._single_swe_touch_scenario_instance_id()
        if isinstance(
            instance_id, str
        ) and self._swe_touch_instance_id_matches_scenarios(instance_id):
            return instance_id
        if scenario_instance_id is not None:
            return scenario_instance_id
        config_instance_id = self._swe_touch_instance_id_from_trial_config()
        if config_instance_id is not None:
            return config_instance_id
        trial_instance_id = self._swe_touch_instance_id_from_trial_name()
        if trial_instance_id is not None:
            return trial_instance_id
        trial_name = self.logs_dir.parent.name
        parts = trial_name.split("__")
        if len(parts) >= 2:
            return "__".join(parts[:2])
        return None

    def _swe_touch_instance_id_from_trial_config(self) -> str | None:
        config_path = self.logs_dir.parent / "config.json"
        if not config_path.exists():
            return None
        try:
            config = json.loads(config_path.read_text())
            task_path = (config.get("task") or {}).get("path")
            if not isinstance(task_path, str):
                return None
            instance_id = Path(task_path).name
            if self._swe_touch_instance_id_matches_scenarios(instance_id):
                return instance_id
        except Exception:
            return None
        return None

    def _swe_touch_instance_id_from_trial_name(self) -> str | None:
        if not self._swe_touch_scenarios_path:
            return None
        trial_prefix = self.logs_dir.parent.name.split("__", 1)[0]
        if not trial_prefix:
            return None
        index_path = Path(self._swe_touch_scenarios_path) / "index.json"
        if not index_path.exists():
            return None
        try:
            import json

            index = json.loads(index_path.read_text())
            candidates: set[str] = set()
            trial_alias = _swe_touch_instance_alias(trial_prefix)
            for item in index.get("scenarios") or []:
                scenario_path_value = item.get("path")
                if not isinstance(scenario_path_value, str):
                    continue
                scenario_path = (
                    Path(self._swe_touch_scenarios_path) / scenario_path_value
                )
                scenario = json.loads(scenario_path.read_text())
                scenario_instance_id = scenario.get("instance_id")
                if not isinstance(scenario_instance_id, str):
                    continue
                scenario_alias = _swe_touch_instance_alias(scenario_instance_id)
                if scenario_alias.startswith(trial_alias) or trial_alias.startswith(
                    scenario_alias
                ):
                    candidates.add(scenario_instance_id)
            if len(candidates) == 1:
                return next(iter(candidates))
        except Exception:
            return None
        return None

    def _single_swe_touch_scenario_instance_id(self) -> str | None:
        if not self._swe_touch_scenarios_path:
            return None
        index_path = Path(self._swe_touch_scenarios_path) / "index.json"
        if not index_path.exists():
            return None
        try:
            import json

            index = json.loads(index_path.read_text())
            instance_ids: set[str] = set()
            for item in index.get("scenarios") or []:
                path = item.get("path")
                if not isinstance(path, str):
                    continue
                scenario_path = Path(self._swe_touch_scenarios_path) / path
                scenario = json.loads(scenario_path.read_text())
                instance_id = scenario.get("instance_id")
                if isinstance(instance_id, str):
                    instance_ids.add(instance_id)
            if len(instance_ids) == 1:
                return next(iter(instance_ids))
        except Exception:
            return None
        return None

    def _swe_touch_instance_id_matches_scenarios(self, instance_id: str) -> bool:
        if not self._swe_touch_scenarios_path:
            return False
        index_path = Path(self._swe_touch_scenarios_path) / "index.json"
        if not index_path.exists():
            return False
        try:
            import json

            index = json.loads(index_path.read_text())
            for item in index.get("scenarios") or []:
                path = item.get("path")
                if not isinstance(path, str):
                    continue
                scenario_path = Path(self._swe_touch_scenarios_path) / path
                scenario = json.loads(scenario_path.read_text())
                scenario_instance_id = scenario.get("instance_id")
                if isinstance(
                    scenario_instance_id, str
                ) and _same_swe_touch_instance_id(instance_id, scenario_instance_id):
                    return True
        except Exception:
            return False
        return False

    def _resolved_swe_touch_log_path(self) -> Path | str | None:
        if not self._swe_touch_scenarios_path:
            return None
        return (
            self._swe_touch_log_path or self.logs_dir / "swe_touch_interventions.jsonl"
        )

    def _get_env(self, key: str) -> str | None:
        if key in self._extra_env:
            return self._extra_env[key]
        return os.environ.get(key)

    async def _remote_template_vars(
        self,
        environment: BaseEnvironment,
    ) -> dict[str, str]:
        keys = {
            "system": "uname -s",
            "release": "uname -r",
            "version": "uname -v",
            "machine": "uname -m",
            "cwd": "pwd",
        }
        values: dict[str, str] = {}
        timeout_sec = int(
            os.environ.get(
                "HARBOR_REMOTE_TEMPLATE_VARS_TIMEOUT_SEC",
                str(min(max(self._command_timeout_sec, 120), 300)),
            )
        )
        for key, command in keys.items():
            try:
                result = await environment.exec(
                    command=command, timeout_sec=timeout_sec
                )
            except Exception as exc:
                self.logger.debug("Failed to read remote template var %s: %s", key, exc)
                values[key] = ""
                continue
            values[key] = (
                (result.stdout or "").strip() if result.return_code == 0 else ""
            )
        return values

    async def _upload_agent_logs(self, environment: BaseEnvironment) -> None:
        for path in [
            self.logs_dir / self._TRAJECTORY_FILENAME,
            self.logs_dir / "external-harness.stdout.log",
            self.logs_dir / "external-harness.stderr.log",
        ]:
            if not path.exists():
                continue
            try:
                await environment.upload_file(path, f"/logs/agent/{path.name}")
            except Exception as exc:
                self.logger.warning("Failed to upload agent log %s: %s", path, exc)


def _same_swe_touch_instance_id(left: str, right: str) -> bool:
    left_alias = _swe_touch_instance_alias(left)
    right_alias = _swe_touch_instance_alias(right)
    return (
        left_alias == right_alias
        or left_alias.startswith(f"{right_alias}__")
        or right_alias.startswith(f"{left_alias}__")
    )


def _swe_touch_instance_alias(instance_id: str) -> str:
    for prefix in ("swebenchpro_", "deepswe_"):
        if instance_id.startswith(prefix):
            return instance_id[len(prefix) :]
    return instance_id
