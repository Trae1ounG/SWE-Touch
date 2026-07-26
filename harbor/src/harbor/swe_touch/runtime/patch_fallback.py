import argparse
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


class ContextPatchError(RuntimeError):
    pass


class DiffLine:
    def __init__(self, kind: str, text: str) -> None:
        self.kind = kind
        self.text = text


class Hunk:
    def __init__(
        self,
        *,
        path: str,
        old_start: int,
        old_count: int,
        lines: Sequence[DiffLine],
    ) -> None:
        self.path = path
        self.old_start = old_start
        self.old_count = old_count
        self.lines = tuple(lines)

    @property
    def old_block(self) -> List[str]:
        return [line.text for line in self.lines if line.kind != "+"]

    @property
    def new_block(self) -> List[str]:
        return [line.text for line in self.lines if line.kind != "-"]


def apply_unified_diff_by_context(*, repository: Path, diff: str) -> int:
    """Apply a unified diff by anchoring hunks to unchanged context lines.

    This is a fallback for a Counter-Edit after an agent has
    modified the same region. When repeated context creates several candidate
    spans, choose the one nearest to the original hunk location.
    """

    applied = 0
    hunks_by_path: Dict[str, List[Hunk]] = {}
    for hunk in parse_unified_diff(diff):
        hunks_by_path.setdefault(hunk.path, []).append(hunk)

    for path, hunks in hunks_by_path.items():
        target = repository / path
        if target.exists():
            lines = target.read_text().splitlines(keepends=True)
        elif all(_is_new_file_hunk(hunk) for hunk in hunks):
            target.parent.mkdir(parents=True, exist_ok=True)
            lines = []
        elif _restore_file_from_git_head(repository, path):
            lines = target.read_text().splitlines(keepends=True)
        else:
            raise ContextPatchError(f"target file does not exist: {path}")
        for hunk in hunks:
            result = _apply_hunk(lines, hunk)
            lines = result.lines
            applied += int(result.applied)
        target.write_text("".join(lines))
    return applied


def _restore_file_from_git_head(repository: Path, path: str) -> bool:
    target = repository / path
    result = subprocess.run(
        ["git", "-C", str(repository), "show", f"HEAD:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result.stdout)
    return True


def parse_unified_diff(diff: str) -> List[Hunk]:
    hunks = []  # type: List[Hunk]
    current_path = None  # type: Optional[str]
    current_header = None  # type: Optional[Tuple[int, int]]
    current_lines = []  # type: List[DiffLine]

    def flush() -> None:
        nonlocal current_header, current_lines
        if current_path is None or current_header is None:
            return
        hunks.append(
            Hunk(
                path=current_path,
                old_start=current_header[0],
                old_count=current_header[1],
                lines=tuple(current_lines),
            )
        )
        current_header = None
        current_lines = []

    for raw_line in diff.splitlines(keepends=True):
        if raw_line.startswith("diff --git "):
            flush()
            current_path = None
            continue
        if raw_line.startswith("--- "):
            continue
        if raw_line.startswith("+++ "):
            path = raw_line[4:].strip()
            current_path = _normalize_diff_path(path)
            continue
        if raw_line.startswith("@@ "):
            flush()
            current_header = _parse_hunk_header(raw_line)
            continue
        if current_header is None:
            continue
        if raw_line.startswith(("+", "-", " ")):
            current_lines.append(DiffLine(raw_line[0], raw_line[1:]))
            continue
        if raw_line.startswith("\\ No newline at end of file"):
            continue
    flush()
    return hunks


class HunkApplyResult:
    def __init__(self, lines: List[str], applied: bool) -> None:
        self.lines = lines
        self.applied = applied


def _apply_hunk(lines: List[str], hunk: Hunk) -> HunkApplyResult:
    old_block = hunk.old_block
    new_block = hunk.new_block

    old_at = _find_exact_block(lines, old_block)
    if old_at is not None:
        return HunkApplyResult(
            lines[:old_at] + new_block + lines[old_at + len(old_block) :],
            True,
        )

    if _find_exact_block(lines, new_block) is not None:
        return HunkApplyResult(lines, False)

    if _is_new_file_hunk(hunk):
        return HunkApplyResult(list(new_block), True)

    span = _find_context_span(lines, hunk)
    if span is None:
        single_line_result = _apply_nearest_single_line_replacement(lines, hunk)
        if single_line_result is not None:
            return single_line_result
    if span is None:
        span = _find_line_window_span(lines, hunk)
    if span is None:
        span = _find_line_window_span(lines, hunk, require_anchor=False)
    if span is None:
        raise ContextPatchError(
            f"could not locate hunk context in {hunk.path}:{hunk.old_start}"
        )
    start, end = span
    return HunkApplyResult(lines[:start] + new_block + lines[end:], True)


def _is_new_file_hunk(hunk: Hunk) -> bool:
    return (
        hunk.old_start == 0
        and hunk.old_count == 0
        and not hunk.old_block
        and bool(hunk.new_block)
    )


def _apply_nearest_single_line_replacement(
    lines: List[str], hunk: Hunk
) -> Optional[HunkApplyResult]:
    removed_positions = [idx for idx, line in enumerate(hunk.lines) if line.kind == "-"]
    added_lines = [line.text for line in hunk.lines if line.kind == "+"]
    if len(removed_positions) != 1 or len(added_lines) != 1:
        return None

    removed_idx = removed_positions[0]
    old_line = hunk.lines[removed_idx].text
    new_line = added_lines[0]
    old_offset = _old_line_count(hunk.lines[:removed_idx])
    target_idx = hunk.old_start - 1 + old_offset
    search_start = max(0, target_idx - 120)
    search_end = min(len(lines), target_idx + 121)

    candidates = [
        idx for idx in range(search_start, search_end) if lines[idx] == old_line
    ]
    if candidates:
        candidates.sort(key=lambda idx: (abs(idx - target_idx), idx))
        if len(candidates) > 1:
            first_distance = abs(candidates[0] - target_idx)
            second_distance = abs(candidates[1] - target_idx)
            if first_distance == second_distance:
                raise ContextPatchError(
                    f"ambiguous single-line replacement in {hunk.path}:{hunk.old_start}"
                )
        replaced = list(lines)
        replaced[candidates[0]] = new_line
        return HunkApplyResult(replaced, True)

    old_stripped = old_line.lstrip(" \t")
    new_stripped = new_line.lstrip(" \t")
    if old_stripped:
        candidates = [
            idx
            for idx in range(search_start, search_end)
            if lines[idx].lstrip(" \t") == old_stripped
        ]
        if candidates:
            candidates.sort(key=lambda idx: (abs(idx - target_idx), idx))
            if len(candidates) > 1:
                first_distance = abs(candidates[0] - target_idx)
                second_distance = abs(candidates[1] - target_idx)
                if first_distance == second_distance:
                    raise ContextPatchError(
                        f"ambiguous stripped single-line replacement in {hunk.path}:{hunk.old_start}"
                    )
            replaced = list(lines)
            replaced[candidates[0]] = (
                _leading_whitespace(lines[candidates[0]]) + new_stripped
            )
            return HunkApplyResult(replaced, True)

    if any(lines[idx] == new_line for idx in range(search_start, search_end)):
        return HunkApplyResult(lines, False)
    if new_stripped and any(
        lines[idx].lstrip(" \t") == new_stripped
        for idx in range(search_start, search_end)
    ):
        return HunkApplyResult(lines, False)
    return None


def _leading_whitespace(line: str) -> str:
    idx = 0
    while idx < len(line) and line[idx] in (" ", "\t"):
        idx += 1
    return line[:idx]


def _find_exact_block(lines: Sequence[str], block: Sequence[str]) -> Optional[int]:
    if not block:
        return None
    for idx in range(0, len(lines) - len(block) + 1):
        if list(lines[idx : idx + len(block)]) == list(block):
            return idx
    return None


def _find_context_span(lines: Sequence[str], hunk: Hunk) -> Optional[Tuple[int, int]]:
    context_positions = [
        idx
        for idx, line in enumerate(hunk.lines)
        if line.kind == " " and line.text.strip()
    ]
    if not context_positions:
        return None

    preferred_start = max(0, hunk.old_start - 80)
    preferred_end = min(len(lines), hunk.old_start + hunk.old_count + 80)
    candidates = _context_span_candidates(
        lines,
        hunk,
        context_positions,
        search_start=preferred_start,
        search_end=preferred_end,
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return _nearest_context_span(candidates, hunk)

    candidates = _context_span_candidates(
        lines,
        hunk,
        context_positions,
        search_start=0,
        search_end=len(lines),
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return _nearest_context_span(candidates, hunk)
    return None


def _nearest_context_span(
    candidates: Sequence[Tuple[int, int]], hunk: Hunk
) -> Tuple[int, int]:
    target_start = max(0, hunk.old_start - 1)
    return sorted(candidates, key=lambda span: (abs(span[0] - target_start), span[0]))[
        0
    ]


def _find_line_window_span(
    lines: Sequence[str], hunk: Hunk, *, require_anchor: bool = True
) -> Optional[Tuple[int, int]]:
    start = hunk.old_start - 1
    end = start + hunk.old_count
    if start < 0 or end > len(lines) or start >= end:
        return None
    window = lines[start:end]
    # This is the last fallback after exact old/new block and unique context
    # matching fail. One surviving old/context line is enough to use the
    # original hunk window when the agent has rewritten the rest of the region.
    if require_anchor and _anchor_score(window, hunk) < 1:
        return None
    return start, end


def _anchor_score(window: Sequence[str], hunk: Hunk) -> int:
    anchors = {
        line.text for line in hunk.lines if line.kind != "+" and line.text.strip()
    }
    return sum(1 for line in anchors if line in window)


def _context_span_candidates(
    lines: Sequence[str],
    hunk: Hunk,
    context_positions: Sequence[int],
    *,
    search_start: int,
    search_end: int,
) -> List[Tuple[int, int]]:
    first_context_hunk_idx = context_positions[0]
    last_context_hunk_idx = context_positions[-1]
    context_lines = [hunk.lines[idx].text for idx in context_positions]
    max_span = max(hunk.old_count + 30, len(hunk.old_block) + 30)
    candidates = []  # type: List[Tuple[int, int]]

    for first_line_idx in range(search_start, search_end):
        if lines[first_line_idx] != context_lines[0]:
            continue
        current_idx = first_line_idx
        matched_line_indices = [first_line_idx]
        ok = True
        for context_line in context_lines[1:]:
            next_idx = _find_next_line(
                lines,
                context_line,
                start=current_idx + 1,
                stop=min(len(lines), first_line_idx + max_span),
            )
            if next_idx is None:
                ok = False
                break
            matched_line_indices.append(next_idx)
            current_idx = next_idx
        if not ok:
            continue

        start = first_line_idx - _old_line_count(hunk.lines[:first_context_hunk_idx])
        end = (
            matched_line_indices[-1]
            + 1
            + _old_line_count(hunk.lines[last_context_hunk_idx + 1 :])
        )
        if start < 0 or end > len(lines) or start >= end:
            continue
        candidates.append((start, end))
    return candidates


def _find_next_line(
    lines: Sequence[str],
    target: str,
    *,
    start: int,
    stop: int,
) -> Optional[int]:
    for idx in range(start, stop):
        if lines[idx] == target:
            return idx
    return None


def _old_line_count(lines: Sequence[DiffLine]) -> int:
    return sum(1 for line in lines if line.kind != "+")


def _parse_hunk_header(header: str) -> Tuple[int, int]:
    match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,\d+)? @@", header)
    if match is None:
        raise ContextPatchError(f"invalid hunk header: {header.strip()}")
    return int(match.group(1)), int(match.group(2) or "1")


def _normalize_diff_path(path: str) -> str:
    if path.startswith("b/"):
        path = path[2:]
    if path.startswith("/testbed/"):
        path = path[len("/testbed/") :]
    return path.lstrip("./")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("diff_path")
    parser.add_argument("--repository", default=".")
    args = parser.parse_args(argv)
    diff = Path(args.diff_path).read_text()
    applied = apply_unified_diff_by_context(repository=Path(args.repository), diff=diff)
    print(f"context fallback applied_or_verified_hunks={applied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
