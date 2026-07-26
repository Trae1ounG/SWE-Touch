from harbor.swe_touch.regions import mine_critical_regions


def test_mine_regions_prefers_cross_model_edit_overlap() -> None:
    rows = [
        {"model": "a", "edits": [{"path": "src/x.py", "lines": [10, 11, 12]}]},
        {"model": "b", "edits": [{"path": "src/x.py", "lines": [11, 12, 13]}]},
        {"model": "c", "edits": [{"path": "src/y.py", "lines": [7]}]},
    ]
    assert mine_critical_regions(rows) == [
        {
            "path": "src/x.py",
            "start_line": 11,
            "end_line": 12,
            "evidence": {
                "kind": "trajectory_edit",
                "models": ["a", "b"],
                "minimum_model_support": 2,
            },
        }
    ]


def test_mine_regions_falls_back_to_reference_patch() -> None:
    diff = (
        "diff --git a/src/x.py b/src/x.py\n"
        "--- a/src/x.py\n"
        "+++ b/src/x.py\n"
        "@@ -9,2 +9,3 @@\n"
        " old\n"
        "+new_one\n"
        "+new_two\n"
    )
    assert mine_critical_regions([], reference_patch=diff) == [
        {
            "path": "src/x.py",
            "start_line": 10,
            "end_line": 11,
            "evidence": {
                "kind": "reference_patch_fallback",
                "models": ["reference_patch"],
                "minimum_model_support": 1,
            },
        }
    ]
