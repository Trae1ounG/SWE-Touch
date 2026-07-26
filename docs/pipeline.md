# Reproduction Pipeline

The construction pipeline is restartable at every stage. Each stage writes immutable artifacts; later stages consume those files instead of rescanning job directories.

## Artifact contracts

| Stage | Input | Output |
|---|---|---|
| Region mining | normalized model trajectories | one critical-region JSON object per task |
| Synthesis | Harbor tasks + critical regions | `/logs/artifacts/swe_touch_candidate.json` |
| Gate | candidate JSON + original Harbor tasks | three verifier outcomes per candidate |
| Assembly | regions + candidates + gates | dataset-record-v1 JSONL |
| Evaluation | dataset-record-v1 JSONL + Harbor tasks | paired Harbor jobs |
| Aggregation | Harbor `result.json` files | per-trial CSV + summary JSON |

## Normalized trajectory input

```json
{
  "instance_id": "org__repo-123",
  "benchmark": "swe_bench_verified",
  "trajectories": [
    {
      "model": "model-a",
      "edits": [{"path": "src/module.py", "lines": [41, 42, 43]}]
    }
  ]
}
```

All supplied trajectories are eligible evidence. The miner first keeps lines edited by at least two models, then falls back to available single-trajectory edit evidence. If no trajectory contains an edit, `--reference-patch` supplies the final fallback. Adjacent lines become one region, and at most eight regions are retained.

## Candidate artifact

Synthesis uses the original Mini-SWE-Agent system prompt and interaction protocol. Harbor appends
the versioned instruction in `harbor/src/harbor/swe_touch/prompts/counter_edit_synthesis_task_instruction.txt` to the task
description; it does not install a separate synthesis system prompt.

The synthesis agent must write `/logs/artifacts/swe_touch_candidate.json` with:

```json
{
  "instance_id": "org__repo-123",
  "candidate_id": "descriptive-stable-id",
  "diff": "diff --git ...",
  "target_regions": [{"path": "src/module.py", "start_line": 41, "end_line": 43}],
  "user_message": "Keep this implementation and continue from it.",
  "wrong_belief": "...",
  "why_it_looks_plausible": "...",
  "expected_failure_mode": "...",
  "self_check": {"user_edit_only": false, "user_edit_plus_reference": false}
}
```

Self-check evidence is advisory. Acceptance is determined only by the independent Harbor gate.

## Independent three-state validation

For a candidate edit `u` and reference repair `g`, the gate runs three fresh task environments:

1. `g` resolves the task;
2. `u` does not resolve the task;
3. applying `u` and then `g` still does not resolve the task.

An apply error or missing verifier result is an infrastructure error, not a failed benchmark outcome. Re-run those gate variants before assembly.

## Evaluation invariants

Paired job generation holds the task set, model, Mini-SWE-Agent interface, step budget, verifier, retries, and concurrency configuration fixed. Counter-Edit adds only the scenario directory and current user simulator. The runtime writes every intervention and simulator output into the trial agent log directory.
