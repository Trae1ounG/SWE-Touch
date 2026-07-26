# SWE-Touch

<p align="center">
  <img src="assets/swe-touch-logo.png" width="800" alt="SWE-Touch logo">
</p>

<p align="center">
  <a href="https://huggingface.co/datasets/Trae1ounG/SWE-Touch"><img src="https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-Dataset-blue" alt="Hugging Face Dataset"></a>
</p>

---

SWE-Touch evaluates coding agents when a user edits the same workspace during an ongoing software task. This repository contains the complete public implementation: task-critical region mining, Counter-Edit synthesis and validation, runtime user interventions, paired evaluation, and result aggregation.

The implementation extends Harbor and preserves the Mini-SWE-Agent system prompt, tool interface, and interaction loop. Model calls use standard LiteLLM provider names and environment variables. The release contains no private endpoints, credentials, internal infrastructure adapters, historical experiment jobs, or paper-analysis code.

## Installation

```bash
git clone https://github.com/Trae1ounG/SWE-Touch.git
cd SWE-Touch
uv sync --project harbor --group dev
uv run --project harbor harbor swe-touch --help
```

## Dataset

Released records can live in a Hugging Face dataset repository and are downloaded directly by the same CLI that runs evaluation:

```bash
uv run --project harbor harbor swe-touch download \
  --repo-id Trae1ounG/SWE-Touch \
  --revision v0.1.0 \
  --output data/v0.1.0

uv run --project harbor harbor swe-touch validate-data data/v0.1.0
```

The source tree currently includes the versioned release bundle in `data/v0.1.0/`, so evaluation does not depend on a Hugging Face upload during development. Each JSONL row contains task-critical regions, the validated user edit, its trigger schedule, and the user-simulator prompt identifier. The exact format is documented in [`schema/`](schema/) and [`docs/data_schema.md`](docs/data_schema.md).

## Run Evaluation

Evaluation compares two runs of the same Harbor tasks:

- **Vanilla** runs Mini-SWE-Agent without user intervention.
- **Counter-Edit** keeps the task, model, tools, verifier, and step budget unchanged. It applies the released user edit when the agent reaches a task-critical region and then generates the user message from the current trajectory context.

```bash
uv run --project harbor harbor swe-touch prepare-paired \
  --tasks /path/to/harbor/tasks \
  --records data/v0.1.0/swe_bench_verified.jsonl \
  --output runs/example-model \
  --model openai/example-model \
  --simulator-model openai/gpt-4o \
  --repetitions 1 \
  --concurrency 4 \
  --step-limit 100

uv run --project harbor harbor swe-touch run-paired runs/example-model
uv run --project harbor harbor swe-touch aggregate runs/example-model/jobs \
  --output runs/example-model/results.csv
```

The aggregator separates unresolved tasks from infrastructure errors and records input, cached-input, and output tokens for each completed trial.

## Build the Dataset

The public construction pipeline has four stages:

1. **Mine task-critical regions.** Merge contiguous edited lines from model trajectories, preferring overlap across trajectories. If no trajectory contains an edit, the optional reference-patch fallback supplies candidate lines.
2. **Generate user edits.** Run the patch generator through the unchanged Mini-SWE-Agent interface. The released task instruction supplies task-critical regions and executable validation commands; it does not replace the agent system prompt.
3. **Validate each edit.** Run three fresh Harbor tasks. The reference repair must pass, the user edit alone must fail, and the user edit followed by the reference repair must also fail.
4. **Assemble records.** Join regions, candidates, and validation outcomes into the same JSONL schema consumed by evaluation.

```bash
# 1. Mine one instance. Repeat or parallelize per instance.
uv run --project harbor harbor swe-touch mine-regions \
  --trajectories artifacts/normalized_trajectories.json \
  --reference-patch artifacts/reference.diff \
  --output artifacts/regions/example.json

# 2. Prepare and run synthesis.
uv run --project harbor harbor swe-touch prepare-synthesis \
  --tasks /path/to/harbor/tasks \
  --records artifacts/regions \
  --output construction/synthesis \
  --model openai/gpt-5.5 \
  --concurrency 4
uv run --project harbor harbor run --config construction/synthesis/synthesis_job.json
uv run --project harbor harbor swe-touch collect-synthesis construction/synthesis/jobs \
  --output construction/candidates.json

# 3. Prepare and run the independent three-state validation.
uv run --project harbor harbor swe-touch build-gate-requests \
  --candidates construction/candidates.json \
  --tasks /path/to/harbor/tasks \
  --output construction/gate_requests.jsonl
uv run --project harbor harbor swe-touch prepare-gate \
  --requests construction/gate_requests.jsonl \
  --output construction/gate \
  --concurrency 8
uv run --project harbor harbor run --config construction/gate/gate_job.json
uv run --project harbor harbor swe-touch collect-gate \
  --jobs construction/gate/jobs \
  --manifest construction/gate/manifest.json \
  --output construction/gates.json

# 4. Produce the exact JSONL consumed by evaluation.
uv run --project harbor harbor swe-touch assemble-dataset \
  --regions artifacts/regions \
  --candidates construction/candidates.json \
  --gates construction/gates.json \
  --output dist/swe_touch.jsonl
uv run --project harbor harbor swe-touch validate-data dist/swe_touch.jsonl
```

See [`docs/pipeline.md`](docs/pipeline.md) for artifact contracts and failure handling.

## Repository Layout

```text
harbor/                        Pinned Harbor fork and Python environment
harbor/src/harbor/swe_touch/  Construction, runtime, validation, and aggregation
harbor/src/harbor/agents/     Mini-SWE-Agent bridge with SWE-Touch interventions
harbor/tests/                  Contract and runtime tests
data/v0.1.0/                  Versioned records mirrored to Hugging Face
schema/                       Public artifact and record schemas
docs/                         Reproduction documentation
```
