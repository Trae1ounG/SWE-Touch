# Data contracts

The final task record is the single source of truth shared by release and evaluation. There is
no experiment-only translation format.

## Pipeline artifacts

### `critical_regions.json`

Schema: `schema/critical-regions-v1.schema.json`.

```json
{
  "schema_version": "1.0.0",
  "benchmark": "swe_bench_verified",
  "instance_id": "org__repo-123",
  "regions": [{"path": "src/a.py", "start_line": 10, "end_line": 20}],
  "evidence": {"source": "trajectory_edit_overlap"}
}
```

### `candidates.json`

The patch generator writes an object with key `candidates`. Each candidate contains
`candidate_id`, `diff`, `target_regions`, `user_message`, `wrong_belief`,
`why_it_looks_plausible`, and `expected_failure_mode`.

Schema: `schema/candidates-v1.schema.json`.

### `gate_results.json`

The validator writes one row per candidate under `candidates`. A row contains `candidate_id`,
`passes_validation`, and the three state results: `reference_only`, `user_edit_only`, and
`user_edit_plus_reference`.

`resolved` is `true` or `false` only when the verifier completed. Missing results
and infrastructure errors are represented as `null` and must be retried before a
candidate can pass validation.

Schema: `schema/gate-results-v1.schema.json`. The assembler recomputes validation from the three
outcomes and does not trust the producer-supplied summary flag.

### Final dataset record

`harbor swe-touch assemble-record` combines the three artifacts into
`schema/dataset-record-v1.schema.json`. Write the output with a `.jsonl` suffix to obtain the
same one-row container used by the release dataset. `harbor swe-touch build-dataset` validates,
deduplicates, and sorts these records without changing their fields.

`harbor swe-touch materialize` consumes that exact record for evaluation. The resulting
scenario directory is a temporary Harbor adapter, not a second dataset or public schema.
The first intervention retains its recorded trigger. Every later intervention uses
an `edit` trigger and therefore waits for the agent to modify the target region.

```bash
uv run --project harbor harbor swe-touch assemble-record \
  --benchmark swe_bench_verified \
  --instance-id owner__repo-123 \
  --regions work/owner__repo-123/critical_regions.json \
  --candidates work/owner__repo-123/candidates.json \
  --gates work/owner__repo-123/gate_results.json \
  --output work/owner__repo-123/final_record.jsonl

uv run --project harbor harbor swe-touch build-dataset \
  --input work/owner__repo-123/final_record.jsonl \
  --output dist/swe_bench_verified.jsonl
```
