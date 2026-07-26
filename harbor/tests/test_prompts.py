import hashlib
import json
from pathlib import Path

from harbor.swe_touch.runtime.user_simulator import (
    load_counter_edit_user_simulator_prompt,
)
from harbor.swe_touch.synthesis import TASK_INSTRUCTION_PATH


PROMPT_DIR = Path(__file__).parents[1] / "src/harbor/swe_touch/prompts"


def test_user_simulator_prompt_matches_release_checksum() -> None:
    manifest = json.loads((PROMPT_DIR / "manifest.json").read_text(encoding="utf-8"))
    config = manifest["user_simulator"]
    content = (PROMPT_DIR / config["system_prompt"]).read_text(encoding="utf-8")
    digest = hashlib.sha256(content.rstrip("\n").encode()).hexdigest()
    assert digest == config["system_prompt_sha256_without_trailing_newline"]
    assert load_counter_edit_user_simulator_prompt() == content.rstrip("\n")


def test_synthesis_task_instruction_matches_manifest() -> None:
    manifest = json.loads((PROMPT_DIR / "manifest.json").read_text(encoding="utf-8"))
    config = manifest["counter_edit_synthesis"]
    content = (PROMPT_DIR / config["task_instruction"]).read_text(encoding="utf-8")
    digest = hashlib.sha256(content.rstrip("\n").encode()).hexdigest()

    assert digest == config["task_instruction_sha256_without_trailing_newline"]
    assert "/logs/artifacts/swe_touch_candidate.json" in content
    assert (
        TASK_INSTRUCTION_PATH.read_bytes()
        == (PROMPT_DIR / config["task_instruction"]).read_bytes()
    )
