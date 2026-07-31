---
pretty_name: SWE-Touch
license: mit
task_categories:
- text-generation
tags:
- software-engineering
- coding-agents
- benchmark
- swe-bench
configs:
- config_name: swe_bench_verified
  data_files:
  - split: test
    path: swe_bench_verified.jsonl
- config_name: swe_bench_pro
  data_files:
  - split: test
    path: swe_bench_pro.jsonl
- config_name: deepswe
  data_files:
  - split: test
    path: deepswe.jsonl
---

# SWE-Touch

SWE-Touch evaluates coding agents when a user edits the same workspace during an
ongoing software task. This release contains 250 validated records spanning
SWE-bench Verified, SWE-Bench Pro, and DeepSWE.

Each record includes task-critical regions, a validated Counter-Edit or text
fallback, its trigger schedule, and the user-simulator prompt identifier. The
construction and evaluation pipeline is available at
[Trae1ounG/SWE-Touch](https://github.com/Trae1ounG/SWE-Touch).

## Configurations

- `swe_bench_verified`: 200 records.
- `swe_bench_pro`: 25 records.
- `deepswe`: 25 records.

All three configurations use the `test` split. `manifest.json` and
`checksums.sha256` identify the exact released files.

## Load

```python
from datasets import load_dataset

records = load_dataset(
    "json",
    data_files={
        "test": "https://huggingface.co/datasets/Trae1ounG/SWE-Touch/resolve/v0.1.2/swe_bench_verified.jsonl",
    },
    split="test",
)
```
