from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path


class UvHarnessRunner:
    """Run a harness module in a uv-managed control-side environment."""

    def __init__(self, uv_binary: str | None = None) -> None:
        self.uv_binary = uv_binary or shutil.which("uv") or "uv"

    async def run_module(
        self,
        *,
        module: str,
        args: list[str],
        packages: list[str],
        logs_dir: Path,
        env: dict[str, str] | None = None,
        python: str | None = None,
        timeout_sec: int | None = None,
    ) -> int:
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = logs_dir / "external-harness.stdout.log"
        stderr_path = logs_dir / "external-harness.stderr.log"

        command = [self.uv_binary, "run", "--no-project"]
        command.extend(["--python", python or sys.executable])
        for package in packages:
            command.extend(["--with", package])
        command.extend(["python", "-m", module, *args])

        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        process_env["PYTHONPATH"] = _prepend_pythonpath(_harbor_src_dir(), process_env)

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            stdout_path.write_bytes(stdout or b"")
            stderr_path.write_bytes(stderr or b"")
            raise

        stdout_path.write_bytes(stdout or b"")
        stderr_path.write_bytes(stderr or b"")
        return int(process.returncode or 0)


def _harbor_src_dir() -> Path:
    return Path(__file__).resolve().parents[3]


def _prepend_pythonpath(path: Path, env: dict[str, str]) -> str:
    existing = env.get("PYTHONPATH")
    if existing:
        return f"{path}{os.pathsep}{existing}"
    return str(path)
