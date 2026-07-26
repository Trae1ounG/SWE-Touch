import json
from pathlib import Path

from harbor.swe_touch.artifacts import (
    assemble_dataset_from_pipeline,
    assemble_record,
    build_dataset,
)
from harbor.swe_touch.io import read_jsonl
from harbor.swe_touch.records import materialize_scenarios


def test_assembled_record_is_directly_evaluable(tmp_path: Path) -> None:
    regions = tmp_path / "regions.json"
    candidates = tmp_path / "candidates.json"
    gates = tmp_path / "gates.json"
    output = tmp_path / "record.json"
    regions.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "instance_id": "owner__repo-1",
                "regions": [{"path": "src/x.py", "start_line": 2, "end_line": 4}],
                "evidence": {"source": "trajectory_edit_overlap"},
            }
        )
    )
    candidates.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "instance_id": "owner__repo-1",
                "candidates": [
                    {
                        "candidate_id": "wrong-guard",
                        "diff": "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n",
                        "target_regions": {"src/x.py": [[2, 4]]},
                    }
                ],
            }
        )
    )
    gates.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "instance_id": "owner__repo-1",
                "candidates": [
                    {
                        "candidate_id": "wrong-guard",
                        "accepted": True,
                        "reference_only": {"resolved": True},
                        "user_edit_only": {"resolved": False},
                        "user_edit_plus_reference": {"resolved": False},
                    }
                ],
            }
        )
    )
    record = assemble_record(
        benchmark="swe_bench_verified",
        instance_id="owner__repo-1",
        regions_path=regions,
        candidates_path=candidates,
        gates_path=gates,
        output_path=output,
        trigger_event="read_or_edit",
        max_interventions=3,
        prompt_id="counter_edit_user_simulator",
    )
    assert record["counter_edit"]["max_interventions"] == 3
    assert [row["order"] for row in record["counter_edit"]["interventions"]] == [
        1,
        2,
        3,
    ]


def test_pipeline_record_is_the_dataset_and_evaluation_record(tmp_path: Path) -> None:
    regions = tmp_path / "regions.json"
    candidates = tmp_path / "candidates.json"
    gates = tmp_path / "gates.json"
    final_record = tmp_path / "final-record.jsonl"
    dataset = tmp_path / "dataset.jsonl"
    scenarios = tmp_path / "scenarios"
    regions.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "instance_id": "example__task-1",
                "regions": [{"path": "src/core.py", "start_line": 10, "end_line": 12}],
                "evidence": {"source": "trajectory_edit_overlap"},
            }
        )
    )
    candidates.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "instance_id": "example__task-1",
                "candidates": [
                    {
                        "candidate_id": "candidate-1",
                        "diff": (
                            "diff --git a/src/core.py b/src/core.py\n"
                            "--- a/src/core.py\n"
                            "+++ b/src/core.py\n"
                            "@@ -10 +10 @@\n-old\n+new\n"
                        ),
                        "target_regions": {"src/core.py": [[10, 12]]},
                    }
                ],
            }
        )
    )
    gates.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "instance_id": "example__task-1",
                "candidates": [
                    {
                        "candidate_id": "candidate-1",
                        "accepted": True,
                        "reference_only": {"resolved": True},
                        "user_edit_only": {"resolved": False},
                        "user_edit_plus_reference": {"resolved": False},
                    }
                ],
            }
        )
    )

    expected = assemble_record(
        benchmark="swe_bench_verified",
        instance_id="example__task-1",
        regions_path=regions,
        candidates_path=candidates,
        gates_path=gates,
        output_path=final_record,
        trigger_event="read_or_edit",
        max_interventions=3,
        prompt_id="counter_edit_user_simulator",
    )
    build_dataset([final_record], dataset)
    assert read_jsonl(dataset) == [expected]

    materialize_scenarios(dataset, scenarios)
    scenario = json.loads(
        (scenarios / "example__task-1" / "round1.json").read_text(encoding="utf-8")
    )
    assert scenario["instance_id"] == expected["instance_id"]
    assert (
        scenario["patch"]["patch_id"]
        == (expected["counter_edit"]["interventions"][0]["patch"]["id"])
    )


def test_assemble_record_recomputes_validation_result(tmp_path: Path) -> None:
    regions = tmp_path / "regions.json"
    candidates = tmp_path / "candidates.json"
    gates = tmp_path / "gates.json"
    output = tmp_path / "record.json"
    regions.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "instance_id": "owner__repo-2",
                "regions": [{"path": "src/x.py", "start_line": 2, "end_line": 4}],
                "evidence": {},
            }
        )
    )
    candidates.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "instance_id": "owner__repo-2",
                "candidates": [
                    {
                        "candidate_id": "bad-gate",
                        "diff": "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n",
                        "target_regions": {"src/x.py": [[2, 4]]},
                    }
                ],
            }
        )
    )
    gates.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "instance_id": "owner__repo-2",
                "candidates": [
                    {
                        "candidate_id": "bad-gate",
                        "accepted": True,
                        "reference_only": {"resolved": True},
                        "user_edit_only": {"resolved": False},
                        "user_edit_plus_reference": {"resolved": True},
                    }
                ],
            }
        )
    )
    record = assemble_record(
        benchmark="swe_bench_verified",
        instance_id="owner__repo-2",
        regions_path=regions,
        candidates_path=candidates,
        gates_path=gates,
        output_path=output,
        trigger_event="read_or_edit",
        max_interventions=3,
        prompt_id="counter_edit_user_simulator",
    )
    assert record["counter_edit"]["mode"] == "text_only"


def test_batch_assembly_accepts_synthesis_region_lists(tmp_path: Path) -> None:
    regions_dir = tmp_path / "regions"
    regions_dir.mkdir()
    (regions_dir / "example.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "benchmark": "swe_bench_verified",
                "instance_id": "example__task-2",
                "regions": [{"path": "src/core.py", "start_line": 4, "end_line": 6}],
                "evidence": {"source": "trajectory_edit_overlap"},
            }
        ),
        encoding="utf-8",
    )
    candidate = {
        "instance_id": "example__task-2",
        "candidate_id": "candidate-list-regions",
        "diff": (
            "diff --git a/src/core.py b/src/core.py\n"
            "--- a/src/core.py\n"
            "+++ b/src/core.py\n"
            "@@ -4 +4 @@\n-old\n+new\n"
        ),
        "target_regions": [{"path": "src/core.py", "start_line": 4, "end_line": 6}],
    }
    candidates = tmp_path / "candidates.json"
    candidates.write_text(
        json.dumps({"schema_version": "1.0.0", "candidates": [candidate]}),
        encoding="utf-8",
    )
    gates = tmp_path / "gates.json"
    gates.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "candidates": [
                    {
                        "instance_id": "example__task-2",
                        "candidate_id": "candidate-list-regions",
                        "reference_only": {"resolved": True},
                        "user_edit_only": {"resolved": False},
                        "user_edit_plus_reference": {"resolved": False},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "dataset.jsonl"

    result = assemble_dataset_from_pipeline(
        regions_path=regions_dir,
        candidates_path=candidates,
        gates_path=gates,
        output_path=output,
    )

    assert result["records"] == 1
    [record] = read_jsonl(output)
    assert record["counter_edit"]["mode"] == "patch"
    assert record["counter_edit"]["interventions"][0]["patch"]["target_regions"] == [
        {"path": "src/core.py", "start_line": 4, "end_line": 6}
    ]
