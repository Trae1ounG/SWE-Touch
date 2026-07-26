from __future__ import annotations

from pathlib import Path
from typing import Any


DATASET_PATTERNS = (
    "*.jsonl",
    "manifest.json",
    "checksums.sha256",
)


def download_dataset(
    *,
    repo_id: str,
    output_dir: Path,
    revision: str | None = None,
) -> dict[str, Any]:
    """Download released SWE-Touch records from a Hugging Face dataset repo."""

    from huggingface_hub import snapshot_download

    path = snapshot_download(
        repo_id=repo_id,
        repo_type="dataset",
        revision=revision,
        allow_patterns=list(DATASET_PATTERNS),
        local_dir=output_dir,
    )
    files = sorted(
        file.relative_to(output_dir).as_posix()
        for file in output_dir.rglob("*")
        if file.is_file()
    )
    return {"repo_id": repo_id, "revision": revision, "path": path, "files": files}
