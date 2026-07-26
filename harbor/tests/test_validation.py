from pathlib import Path

import pytest

from harbor.swe_touch.validation import scan_sensitive_text, validate_record


def test_sensitive_scan_rejects_internal_path() -> None:
    with pytest.raises(ValueError, match="private filesystem path"):
        scan_sensitive_text("/mnt/bn/private/run", source="sample")


def test_sensitive_scan_accepts_repository_diff() -> None:
    scan_sensitive_text("diff --git a/src/a.py b/src/a.py", source=str(Path("sample")))


def test_record_rejects_noncanonical_prompt_id() -> None:
    record = {
        "schema_version": "1.0.0",
        "benchmark": "swe_bench_verified",
        "instance_id": "example__task-1",
        "task_critical_regions": [{"path": "src/a.py", "start_line": 1, "end_line": 2}],
        "counter_edit": {
            "message_prompt_id": "old_prompt",
            "max_interventions": 0,
            "interventions": [],
        },
    }

    with pytest.raises(ValueError, match="unsupported user simulator prompt"):
        validate_record(record)
