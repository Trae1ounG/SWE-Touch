from __future__ import annotations

import asyncio
import concurrent.futures
import json
import secrets
import shlex
import tempfile
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from harbor.environments.base import BaseEnvironment
from harbor.swe_touch.runtime.remote import CounterEditController


SWE_TOUCH_MESSAGE_INJECTION_FORMAT = "user_role_message"
SWE_TOUCH_USER_MESSAGES_FIELD = "swe_touch_user_messages"


class EnvironmentBridgeServer:
    """Small localhost bridge from an external harness process to BaseEnvironment."""

    def __init__(
        self,
        *,
        environment: BaseEnvironment,
        template_vars: dict[str, Any],
        swe_touch_controller: CounterEditController | None = None,
        swe_touch_log_path: Path | str | None = None,
    ) -> None:
        self.environment = environment
        self.template_vars = template_vars
        self.swe_touch_controller = swe_touch_controller
        self.swe_touch_log_path = (
            Path(swe_touch_log_path) if swe_touch_log_path else None
        )
        self.token = secrets.token_urlsafe(24)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._swe_touch_log_lock = threading.Lock()
        self._recent_agent_events_lock = threading.Lock()
        self._recent_agent_events: list[dict[str, Any]] = []
        self._agent_command_count_lock = threading.Lock()
        self._agent_command_count = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("Bridge server has not started.")
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        handler = self._make_handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="harbor-environment-bridge",
            daemon=True,
        )
        self._thread.start()

    async def stop(self) -> None:
        if self._server is None:
            return
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                if self.headers.get("Authorization") != f"Bearer {bridge.token}":
                    self.send_error(401)
                    return

                length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                except json.JSONDecodeError:
                    self.send_error(400, "Invalid JSON")
                    return

                try:
                    if self.path == "/exec":
                        response = bridge._handle_exec(payload)
                    elif self.path == "/execute":
                        response = bridge._handle_execute(payload)
                    elif self.path == "/template-vars":
                        response = bridge.template_vars
                    elif self.path == "/read-file":
                        response = bridge._handle_read_file(payload)
                    elif self.path == "/write-file":
                        response = bridge._handle_write_file(payload)
                    elif self.path == "/upload":
                        response = bridge._handle_upload(payload)
                    else:
                        self.send_error(404)
                        return
                except Exception as exc:
                    self._write_json(
                        500,
                        {
                            "error": str(exc),
                            "error_type": exc.__class__.__name__,
                        },
                    )
                    return

                self._write_json(200, response)

            def _write_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def _handle_exec(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._loop is None:
            raise RuntimeError("Bridge server loop is not available.")
        command = payload.get("command")
        if not isinstance(command, str):
            raise ValueError("exec payload requires string field 'command'.")

        timeout_sec = payload.get("timeout_sec")
        future = asyncio.run_coroutine_threadsafe(
            self.environment.exec(
                command=command,
                cwd=payload.get("cwd"),
                env=payload.get("env"),
                timeout_sec=timeout_sec,
                user=payload.get("user"),
            ),
            self._loop,
        )
        try:
            result = future.result(
                timeout=(timeout_sec or 0) + 30 if timeout_sec else None
            )
        except (concurrent.futures.TimeoutError, TimeoutError, asyncio.TimeoutError):
            future.cancel()
            response = {
                "stdout": "",
                "stderr": f"Command timed out after {timeout_sec} seconds.",
                "return_code": 124,
            }
        else:
            response = {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "return_code": result.return_code,
            }
        return self._apply_swe_touch_intervention(
            payload,
            response,
            command_result=dict(response),
        )

    def _apply_swe_touch_intervention(
        self,
        payload: dict[str, Any],
        response: dict[str, Any],
        *,
        command_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        command_index = self._next_agent_command_index()
        intervention = self._maybe_apply_swe_touch(
            payload,
            command_index=command_index,
            command_result=command_result,
        )
        if intervention is not None:
            if self._swe_touch_scenario_already_recorded(intervention.scenario_id):
                self._record_agent_event(payload, command_result or response)
                return response
            if intervention.message_visible:
                response[SWE_TOUCH_USER_MESSAGES_FIELD] = [intervention.message]
            self._record_swe_touch_intervention(
                payload,
                intervention,
                command_index=command_index,
            )
        self._record_agent_event(payload, command_result or response)
        return response

    def _maybe_apply_swe_touch(
        self,
        payload: dict[str, Any],
        *,
        command_index: int,
        command_result: dict[str, Any] | None = None,
    ) -> Any | None:
        if self.swe_touch_controller is None:
            return None
        if self._loop is None:
            raise RuntimeError("Bridge server loop is not available.")
        command = payload.get("command")
        if not isinstance(command, str):
            return None
        future = asyncio.run_coroutine_threadsafe(
            self.swe_touch_controller.maybe_intervene(
                command=command,
                cwd=payload.get("cwd"),
                environment=self.environment,
                blocked_scenario_ids=self._recorded_swe_touch_scenario_ids(),
                command_result=command_result,
                recent_events=self._recent_agent_events_snapshot(),
                command_index=command_index,
            ),
            self._loop,
        )
        try:
            return future.result(timeout=90)
        except (concurrent.futures.TimeoutError, TimeoutError, asyncio.TimeoutError):
            future.cancel()
            self._record_swe_touch_timeout(payload)
            return None

    def _next_agent_command_index(self) -> int:
        with self._agent_command_count_lock:
            self._agent_command_count += 1
            return self._agent_command_count

    def _record_swe_touch_timeout(self, payload: dict[str, Any]) -> None:
        if self.swe_touch_log_path is None:
            return
        row = {
            "event_type": "swe_touch_intervention_timeout",
            "created_at": datetime.now(UTC).isoformat(),
            "command": payload.get("command"),
            "cwd": payload.get("cwd"),
            "timeout_sec": 90,
        }
        with self._swe_touch_log_lock:
            self.swe_touch_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.swe_touch_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _recorded_swe_touch_scenario_ids(self) -> set[str]:
        if self.swe_touch_log_path is None:
            return set()
        with self._swe_touch_log_lock:
            if not self.swe_touch_log_path.exists():
                return set()
            scenario_ids: set[str] = set()
            for line in self.swe_touch_log_path.read_text(
                encoding="utf-8"
            ).splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                scenario_id = row.get("scenario_id")
                if isinstance(scenario_id, str):
                    scenario_ids.add(scenario_id)
            return scenario_ids

    def _swe_touch_scenario_already_recorded(self, scenario_id: str) -> bool:
        return scenario_id in self._recorded_swe_touch_scenario_ids()

    def _record_swe_touch_intervention(
        self,
        payload: dict[str, Any],
        intervention: Any,
        *,
        command_index: int | None = None,
    ) -> None:
        if self.swe_touch_log_path is None:
            return
        row = {
            "event_type": "swe_touch_intervention",
            "created_at": datetime.now(UTC).isoformat(),
            "scenario_id": intervention.scenario_id,
            "instance_id": intervention.instance_id,
            "patch_kind": intervention.patch_kind,
            "patch_id": intervention.patch_id,
            "patch_diff_sha256": intervention.patch_diff_sha256,
            "patch_source": intervention.patch_source,
            "patch_applied": intervention.patch_applied,
            "patch_error": intervention.patch_error,
            "message_visible": intervention.message_visible,
            "intervention_mode": intervention.intervention_mode,
            "message_source": intervention.message_source,
            "message_prompt_id": intervention.message_prompt_id,
            "message_prompt_sha256": intervention.message_prompt_sha256,
            "message_model": intervention.message_model,
            "message_fallback_reason": intervention.message_fallback_reason,
            "simulator_raw_output": intervention.simulator_raw_output,
            "intervention_index": intervention.intervention_index,
            "policy_id": intervention.policy_id,
            "validation_status": intervention.validation_status,
            "trigger_type": getattr(intervention, "trigger_type", None),
            "trigger_event": getattr(intervention, "trigger_event", None),
            "trigger_regions": list(getattr(intervention, "trigger_regions", ())),
            "message": intervention.message,
            "message_injection_format": (
                SWE_TOUCH_MESSAGE_INJECTION_FORMAT
                if intervention.message_visible
                else None
            ),
            "command": payload.get("command"),
            "command_index": command_index,
            "cwd": payload.get("cwd"),
        }
        self.swe_touch_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._swe_touch_log_lock:
            with self.swe_touch_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _recent_agent_events_snapshot(self) -> tuple[dict[str, Any], ...]:
        with self._recent_agent_events_lock:
            return tuple(dict(event) for event in self._recent_agent_events)

    def _record_agent_event(
        self,
        payload: dict[str, Any],
        command_result: dict[str, Any] | None,
    ) -> None:
        command = payload.get("command")
        if not isinstance(command, str):
            return
        event: dict[str, Any] = {
            "command": command,
            "cwd": payload.get("cwd"),
        }
        if command_result:
            for key in ("return_code", "exit_code", "write_file"):
                if key in command_result:
                    event[key] = command_result[key]
            for key in ("stdout", "stderr", "content"):
                value = command_result.get(key)
                if isinstance(value, str):
                    event[key] = _compact_event_text(value, limit=600)
        with self._recent_agent_events_lock:
            self._recent_agent_events.append(event)
            del self._recent_agent_events[:-8]

    def _handle_execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._handle_exec(
            {
                "command": _command_to_shell(
                    payload.get("command"), payload.get("shell")
                ),
                "cwd": payload.get("cwd"),
                "env": payload.get("env"),
                "timeout_sec": payload.get("timeout"),
            }
        )
        execute_response = {
            "stdout": response["stdout"],
            "stderr": response["stderr"],
            "exit_code": response["return_code"],
        }
        if SWE_TOUCH_USER_MESSAGES_FIELD in response:
            execute_response[SWE_TOUCH_USER_MESSAGES_FIELD] = response[
                SWE_TOUCH_USER_MESSAGES_FIELD
            ]
        return execute_response

    def _handle_read_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._loop is None:
            raise RuntimeError("Bridge server loop is not available.")
        path = payload.get("path")
        if not isinstance(path, str):
            raise ValueError("read-file payload requires string field 'path'.")

        encoding = payload.get("encoding") or "utf-8"
        errors = payload.get("errors")
        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path = Path(tmp_dir) / "downloaded"
            future = asyncio.run_coroutine_threadsafe(
                self.environment.download_file(path, local_path),
                self._loop,
            )
            future.result()
            response = {
                "content": local_path.read_text(encoding=encoding, errors=errors)
            }
            return self._apply_swe_touch_intervention(
                {
                    "command": f"str_replace_editor view {shlex.quote(path)}",
                    "cwd": None,
                },
                response,
                command_result=dict(response),
            )

    def _handle_write_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._loop is None:
            raise RuntimeError("Bridge server loop is not available.")
        path = payload.get("path")
        content = payload.get("content")
        if not isinstance(path, str):
            raise ValueError("write-file payload requires string field 'path'.")
        if not isinstance(content, str):
            raise ValueError("write-file payload requires string field 'content'.")

        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path = Path(tmp_dir) / "upload"
            local_path.write_text(content)
            future = asyncio.run_coroutine_threadsafe(
                self.environment.upload_file(local_path, path),
                self._loop,
            )
            future.result()
        return self._apply_swe_touch_intervention(
            {
                "command": f"str_replace_editor str_replace {shlex.quote(path)}",
                "cwd": None,
            },
            {},
            command_result={"write_file": path},
        )

    def _handle_upload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._loop is None:
            raise RuntimeError("Bridge server loop is not available.")
        source_path = payload.get("source_path")
        target_path = payload.get("target_path")
        if not isinstance(source_path, str):
            raise ValueError("upload payload requires string field 'source_path'.")
        if not isinstance(target_path, str):
            raise ValueError("upload payload requires string field 'target_path'.")

        source = Path(source_path)
        if source.is_dir():
            future = asyncio.run_coroutine_threadsafe(
                self.environment.upload_dir(source, target_path),
                self._loop,
            )
        else:
            future = asyncio.run_coroutine_threadsafe(
                self.environment.upload_file(source, target_path),
                self._loop,
            )
        future.result()
        return {}


def _command_to_shell(command: Any, shell: bool | None) -> str:
    if isinstance(command, str):
        return command
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        if shell:
            return " ".join(command)
        import shlex

        return " ".join(shlex.quote(part) for part in command)
    raise ValueError("execute payload requires string or string-list field 'command'.")


def _compact_event_text(text: str, *, limit: int) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."
