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

Requirements: Python 3.12 or newer, [uv](https://docs.astral.sh/uv/), Git, and
Docker. Harbor starts the task containers; model requests are made through
[LiteLLM](https://docs.litellm.ai/docs/providers).

```bash
git clone https://github.com/Trae1ounG/SWE-Touch.git
cd SWE-Touch
export UV_NO_DEV=1
uv sync --project harbor --locked
uv run --project harbor harbor swe-touch --help
```

Contributors running the test suite should additionally install the development group:

```bash
unset UV_NO_DEV
uv sync --project harbor --group dev --locked
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

The source tree also includes the versioned release bundle in `data/v0.1.0/`, so
evaluation does not depend on Hugging Face availability. Version `v0.1.0` contains
200 SWE-bench Verified records, 25 SWE-Bench Pro records, and 25 DeepSWE records.
Of these, 242 contain independently validated code edits and eight contain the
documented message-only fallback. Each JSONL row contains task-critical regions,
the user edit or fallback, its trigger schedule, validation outcomes, and the
user-simulator prompt identifier. The exact format is documented in
[`schema/`](schema/) and [`docs/data_schema.md`](docs/data_schema.md).

## Prepare Benchmark Tasks

The JSONL release stores SWE-Touch interventions, not third-party repository images
or tests. Obtain the corresponding Harbor tasks from their original projects:

```bash
mkdir -p tasks external

# SWE-bench Verified and SWE-Bench Pro are pinned in Harbor's registry.
uv run --project harbor harbor download swebench-verified@1.0 \
  --output-dir tasks --export
uv run --project harbor harbor download swebenchpro@1.0 \
  --output-dir tasks --export

# DeepSWE is already distributed in Harbor task format.
git clone https://github.com/datacurve-ai/deep-swe.git external/deep-swe
git -C external/deep-swe checkout e016041a6ccf8da29906afc9a3f5a8df940a1f78
```

Use `tasks/swebench-verified`, `tasks/swebenchpro`, or
`external/deep-swe/tasks` as `--tasks`. `prepare-paired` resolves every release
`instance_id` against that directory and writes the exact released subset into both
job configurations. It fails before evaluation if a task is missing or ambiguous.

## Run Evaluation

Evaluation compares two runs of the same Harbor tasks:

- **Vanilla** runs Mini-SWE-Agent without user intervention.
- **Counter-Edit** keeps the task, model, tools, verifier, and step budget unchanged. It applies the released user edit when the agent reaches a task-critical region and then generates the user message from the current trajectory context. The message enters the next model turn as a separate `role=user` message; it is never embedded in a tool call or tool output.

The first intervention follows the release record's trigger (`read_or_edit` for
SWE-bench Verified and `edit` for the harder-task records). Later interventions
wait until the agent actually edits the target region. A patch-application failure
does not consume an intervention or emit a user message; the runtime retries after
the next matching edit.

```bash
export OPENAI_API_KEY=...  # Example; use the variable required by your provider.

uv run --project harbor harbor swe-touch prepare-paired \
  --tasks tasks/swebench-verified \
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

Model names and credentials follow LiteLLM conventions. `openai/example-model` is
only a placeholder; replace it with an endpoint available to you. The simulator can
use a different provider by changing `--simulator-model` and setting that provider's
credential variables. Reproducing the task protocol does not require private
infrastructure, but reproducing a proprietary model's exact score requires access to
the same model version and is subject to provider-side nondeterminism.

For an OpenAI-compatible endpoint that exposes only the Responses API, set
`SWE_TOUCH_SIMULATOR_RESPONSES_BASE_URL` to the API root (the client appends
`/responses`) and `SWE_TOUCH_SIMULATOR_API_KEY` to its credential. The simulator
then uses the selected `--simulator-model` through the Responses API; otherwise it
uses the default LiteLLM chat-completions client.

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

SWE-Touch source code is released under MIT (`LICENSE`). The vendored Harbor fork
retains its Apache-2.0 license in `harbor/LICENSE`.
