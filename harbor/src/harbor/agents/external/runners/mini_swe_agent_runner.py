from __future__ import annotations

import argparse
import contextlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class HarborBridgeEnvironment:
    def __init__(
        self,
        *,
        bridge_url: str,
        bridge_token: str,
        timeout: int,
        bridge_max_retries: int = 3,
        bridge_retry_sleep_sec: float = 5.0,
    ) -> None:
        self.bridge_url = bridge_url.rstrip("/")
        self.bridge_token = bridge_token
        self.config = {"timeout": timeout}
        self.bridge_max_retries = max(1, bridge_max_retries)
        self.bridge_retry_sleep_sec = max(0.0, bridge_retry_sleep_sec)

    def execute(
        self,
        action: dict[str, Any],
        cwd: str = "",
        *,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        command = action.get("command", "")
        try:
            response = self._post(
                "/exec",
                {
                    "command": command,
                    "cwd": cwd or None,
                    "timeout_sec": timeout or self.config["timeout"],
                },
            )
        except urllib.error.HTTPError as exc:
            response = {
                "stdout": "",
                "stderr": _format_http_error(exc),
                "return_code": -1,
            }
        stdout = response.get("stdout") or ""
        stderr = response.get("stderr") or ""
        output = stdout if not stderr else f"{stdout}{stderr}"
        result = {
            "output": output,
            "returncode": response.get("return_code", -1),
            "exception_info": "",
        }
        self._check_finished(result)
        return result

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        from minisweagent.utils.serialize import recursive_merge

        return recursive_merge(
            self.config,
            self._post("/template-vars", {}),
            os.environ,
            kwargs,
        )

    def serialize(self) -> dict[str, Any]:
        return {
            "info": {
                "config": {
                    "environment": self.config,
                    "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.bridge_url}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self.bridge_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        last_exc: Exception | None = None
        for attempt in range(1, self.bridge_max_retries + 1):
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=max(self.config["timeout"], 30) + 30,
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if (
                    not _is_retryable_http_error(exc)
                    or attempt == self.bridge_max_retries
                ):
                    raise
                last_exc = exc
            except urllib.error.URLError as exc:
                if attempt == self.bridge_max_retries:
                    raise
                last_exc = exc

            if self.bridge_retry_sleep_sec:
                time.sleep(self.bridge_retry_sleep_sec)

        assert last_exc is not None
        raise last_exc

    def _check_finished(self, output: dict[str, Any]) -> None:
        from minisweagent.exceptions import Submitted

        lines = output.get("output", "").lstrip().splitlines(keepends=True)
        if (
            lines
            and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
            and output["returncode"] == 0
        ):
            submission = "".join(lines[1:])
            if not submission.strip():
                submission = self._collect_submission_diff()
            raise Submitted(
                {
                    "role": "exit",
                    "content": submission,
                    "extra": {
                        "exit_status": "Submitted",
                        "submission": submission,
                    },
                }
            )

    def _collect_submission_diff(self) -> str:
        try:
            response = self._post(
                "/exec",
                {
                    "command": "git -C /testbed diff --binary",
                    "cwd": None,
                    "timeout_sec": min(self.config["timeout"], 300),
                },
            )
        except Exception:
            return ""
        stdout = response.get("stdout") or ""
        stderr = response.get("stderr") or ""
        if response.get("return_code", -1) != 0:
            return ""
        return stdout if not stderr else f"{stdout}{stderr}"


def _format_http_error(exc: urllib.error.HTTPError) -> str:
    body = ""
    with contextlib.suppress(Exception):
        body = exc.read().decode("utf-8", errors="replace")
    if body:
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(body)
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return f"Bridge HTTP {exc.code} {exc.reason}: {body}".strip()


def _is_retryable_http_error(exc: urllib.error.HTTPError) -> bool:
    if exc.code in {408, 429, 500, 502, 503, 504}:
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-url", required=True)
    parser.add_argument("--bridge-token", required=True)
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--config", action="append", default=["mini"])
    parser.add_argument("--model-class")
    parser.add_argument("--cost-limit", type=float)
    parser.add_argument("--step-limit", type=int)
    parser.add_argument("--command-timeout-sec", type=int, default=30)
    parser.add_argument("--model-request-timeout-sec", type=int)
    parser.add_argument("--model-max-tokens", type=int)
    parser.add_argument("--model-max-retries", type=int)
    parser.add_argument("--model-retry-initial-sleep-sec", type=float)
    parser.add_argument("--model-retry-max-sleep-sec", type=float)
    parser.add_argument("--bridge-max-retries", type=int, default=3)
    parser.add_argument("--bridge-retry-sleep-sec", type=float, default=5.0)
    args = parser.parse_args()

    from minisweagent.agents import get_agent
    from minisweagent.config import get_config_from_spec
    from minisweagent.models import get_model
    from minisweagent.utils.serialize import recursive_merge

    configs = [get_config_from_spec(spec) for spec in args.config]
    agent_config: dict[str, Any] = {
        "mode": "yolo",
        "confirm_exit": False,
        "output_path": Path(args.output_path),
    }
    if args.cost_limit is not None:
        agent_config["cost_limit"] = args.cost_limit
    if args.step_limit is not None:
        agent_config["step_limit"] = args.step_limit

    model_config: dict[str, Any] = {"model_name": args.model}
    if args.model_class:
        model_config["model_class"] = args.model_class
    if args.model_request_timeout_sec is not None:
        model_config["request_timeout_sec"] = args.model_request_timeout_sec
    if args.model_max_tokens is not None:
        model_config["max_tokens"] = args.model_max_tokens
    if args.model_max_retries is not None:
        model_config["max_retries"] = args.model_max_retries
    if args.model_retry_initial_sleep_sec is not None:
        model_config["retry_initial_sleep_sec"] = args.model_retry_initial_sleep_sec
    if args.model_retry_max_sleep_sec is not None:
        model_config["retry_max_sleep_sec"] = args.model_retry_max_sleep_sec

    config = recursive_merge(
        *configs,
        {
            "agent": agent_config,
            "model": model_config,
        },
    )

    model = get_model(config=config.get("model", {}))
    env = HarborBridgeEnvironment(
        bridge_url=args.bridge_url,
        bridge_token=args.bridge_token,
        timeout=args.command_timeout_sec,
        bridge_max_retries=args.bridge_max_retries,
        bridge_retry_sleep_sec=args.bridge_retry_sleep_sec,
    )
    agent = get_agent(model, env, config.get("agent", {}), default_type="interactive")
    agent.run(Path(args.task_file).read_text())


if __name__ == "__main__":
    main()
