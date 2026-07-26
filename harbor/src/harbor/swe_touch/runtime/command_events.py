from __future__ import annotations

import re
import shlex

from harbor.swe_touch.runtime.schemas import AgentEvent


def events_from_shell_command(command: str) -> list[AgentEvent]:
    """Best-effort normalization from shell commands to code-region events."""

    events: list[AgentEvent] = []
    events.extend(_str_replace_editor_events(command))
    events.extend(_sed_events(command))
    events.extend(_sed_edit_events(command))
    events.extend(_plain_file_read_events(command))
    events.extend(_search_read_events(command))
    events.extend(_python_file_events(command))
    events.extend(_git_file_mutation_events(command))
    events.extend(_file_mutation_events(command))
    events.extend(_write_events(command))
    return _dedupe(events)


def _str_replace_editor_events(command: str) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return events

    for idx, token in enumerate(tokens[:-2]):
        if token != "str_replace_editor":
            continue
        action = tokens[idx + 1]
        path = _clean_path(tokens[idx + 2])
        if not path:
            continue
        if action == "view":
            start_line, end_line = _view_range(tokens[idx + 3 :])
            events.append(
                AgentEvent(
                    event_type="read",
                    path=path,
                    start_line=start_line,
                    end_line=end_line,
                    command=command,
                )
            )
        elif action in {"str_replace", "create", "insert"}:
            events.append(AgentEvent(event_type="edit", path=path, command=command))
    return events


def _view_range(tokens: list[str]) -> tuple[int | None, int | None]:
    for idx, token in enumerate(tokens[:-2]):
        if token != "--view_range":
            continue
        try:
            return int(tokens[idx + 1]), int(tokens[idx + 2])
        except ValueError:
            return None, None
    return None, None


def _sed_events(command: str) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    pattern = re.compile(r"sed\s+-n\s+['\"]?(\d+),(\d+)p['\"]?\s+([^\s;&|]+)")
    for match in pattern.finditer(command):
        path = _clean_path(match.group(3))
        if path:
            events.append(
                AgentEvent(
                    event_type="read",
                    path=path,
                    start_line=int(match.group(1)),
                    end_line=int(match.group(2)),
                    command=command,
                )
            )
    return events


def _sed_edit_events(command: str) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return events

    for idx, token in enumerate(tokens):
        if token != "sed":
            continue

        cursor = idx + 1
        in_place = False
        while cursor < len(tokens) and tokens[cursor].startswith("-"):
            option = tokens[cursor]
            if option == "-i" or option.startswith("-i"):
                in_place = True
            cursor += 1

        if not in_place:
            continue

        if cursor >= len(tokens):
            continue

        # Skip the sed script and treat subsequent path-like arguments as edited files.
        cursor += 1
        while cursor < len(tokens) and tokens[cursor] not in {"&&", ";", "|"}:
            path = _clean_path(tokens[cursor])
            if path and _looks_like_repo_file(path):
                events.append(AgentEvent(event_type="edit", path=path, command=command))
            cursor += 1
    return events


def _plain_file_read_events(command: str) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return events
    for idx, token in enumerate(tokens[:-1]):
        if token in {"cat", "head", "tail", "nl"}:
            path = _first_path_token(tokens[idx + 1 :])
            if path:
                events.append(AgentEvent(event_type="read", path=path, command=command))
    return events


def _write_events(command: str) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    for pattern in [
        re.compile(r">+\s*([/\w.\-]+)"),
        re.compile(r"(?:tee|install)\s+(?:-[^\s]+\s+)*([/\w.\-]+)"),
    ]:
        for match in pattern.finditer(command):
            path = _clean_path(match.group(1))
            if path and _looks_like_repo_file(path):
                events.append(AgentEvent(event_type="edit", path=path, command=command))
    return events


def _search_read_events(command: str) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return events
    for idx, token in enumerate(tokens):
        if token not in {"rg", "grep"}:
            continue
        for candidate in tokens[idx + 1 :]:
            if candidate in {"&&", ";", "|"}:
                break
            if candidate.startswith("-"):
                continue
            path = _clean_path(candidate)
            if path and _looks_like_repo_file(path):
                events.append(AgentEvent(event_type="read", path=path, command=command))
    return events


def _git_file_mutation_events(command: str) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return events

    idx = 0
    while idx < len(tokens):
        if tokens[idx] != "git":
            idx += 1
            continue

        subcommand_idx = idx + 1
        if subcommand_idx < len(tokens) and tokens[subcommand_idx] == "-C":
            subcommand_idx += 2
        if subcommand_idx >= len(tokens):
            idx += 1
            continue

        subcommand = tokens[subcommand_idx]
        if subcommand == "checkout":
            path_tokens = _git_checkout_path_tokens(tokens[subcommand_idx + 1 :])
        elif subcommand == "restore":
            path_tokens = _git_restore_path_tokens(tokens[subcommand_idx + 1 :])
        else:
            idx += 1
            continue

        for token in path_tokens:
            path = _clean_path(token)
            if path and _looks_like_repo_file(path):
                events.append(AgentEvent(event_type="edit", path=path, command=command))
        idx = subcommand_idx + 1
    return events


def _git_checkout_path_tokens(tokens: list[str]) -> list[str]:
    if "--" in tokens:
        return _tokens_until_shell_delimiter(tokens[tokens.index("--") + 1 :])
    paths: list[str] = []
    for token in _tokens_until_shell_delimiter(tokens):
        if token.startswith("-"):
            continue
        if _looks_like_repo_file(token):
            paths.append(token)
    return paths


def _git_restore_path_tokens(tokens: list[str]) -> list[str]:
    paths: list[str] = []
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token in {"&&", ";", "|"}:
            break
        if token == "--":
            paths.extend(_tokens_until_shell_delimiter(tokens[idx + 1 :]))
            break
        if token.startswith("--source="):
            idx += 1
            continue
        if token in {"--source", "--worktree", "--staged"}:
            idx += 2 if token == "--source" else 1
            continue
        if token.startswith("-"):
            idx += 1
            continue
        paths.append(token)
        idx += 1
    return paths


def _file_mutation_events(command: str) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return events

    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token in {"rm", "touch"}:
            for candidate in _command_path_args(tokens[idx + 1 :]):
                path = _clean_path(candidate)
                if path and _looks_like_repo_file(path):
                    events.append(
                        AgentEvent(event_type="edit", path=path, command=command)
                    )
        elif token in {"cp", "mv"}:
            path_args = _command_path_args(tokens[idx + 1 :])
            if path_args:
                path = _clean_path(path_args[-1])
                if path and _looks_like_repo_file(path):
                    events.append(
                        AgentEvent(event_type="edit", path=path, command=command)
                    )
        idx += 1
    return events


def _command_path_args(tokens: list[str]) -> list[str]:
    paths: list[str] = []
    for token in _tokens_until_shell_delimiter(tokens):
        if token.startswith("-"):
            continue
        paths.append(token)
    return paths


def _tokens_until_shell_delimiter(tokens: list[str]) -> list[str]:
    result: list[str] = []
    for token in tokens:
        if token in {"&&", ";", "|"}:
            break
        result.append(token)
    return result


def _python_file_events(command: str) -> list[AgentEvent]:
    positioned_events: list[tuple[int, AgentEvent]] = []
    for match in re.finditer(
        r"Path\(['\"]([^'\"]+)['\"]\)\.(read_text|write_text)\(",
        command,
    ):
        path = _clean_path(match.group(1))
        if path:
            event_type = "read" if match.group(2) == "read_text" else "edit"
            positioned_events.append(
                (
                    match.start(),
                    AgentEvent(event_type=event_type, path=path, command=command),
                )
            )

    path_vars: dict[str, str] = {}
    var_events: list[tuple[int, str, re.Match[str]]] = []
    var_events.extend(
        (match.start(), "assign", match)
        for match in re.finditer(
            r"\b([A-Za-z_]\w*)\s*=\s*Path\(['\"]([^'\"]+)['\"]\)",
            command,
        )
    )
    var_events.extend(
        (match.start(), "method", match)
        for match in re.finditer(
            r"\b([A-Za-z_]\w*)\.(read_text|write_text)\(",
            command,
        )
    )
    for _, event_kind, match in sorted(var_events, key=lambda item: item[0]):
        if event_kind == "assign":
            path = _clean_path(match.group(2))
            if path:
                path_vars[match.group(1)] = path
            continue

        path = path_vars.get(match.group(1))
        if not path:
            continue
        event_type = "read" if match.group(2) == "read_text" else "edit"
        positioned_events.append(
            (
                match.start(),
                AgentEvent(event_type=event_type, path=path, command=command),
            )
        )

    string_vars: dict[str, str] = {}
    string_var_events: list[tuple[int, str, re.Match[str]]] = []
    string_var_events.extend(
        (match.start(), "assign", match)
        for match in re.finditer(
            r"\b([A-Za-z_]\w*)\s*=\s*['\"]([^'\"]+)['\"]",
            command,
        )
    )
    string_var_events.extend(
        (match.start(), "open", match)
        for match in re.finditer(
            r"open\(\s*([A-Za-z_]\w*)\s*,\s*['\"]([^'\"]*)['\"]",
            command,
        )
    )
    for _, event_kind, match in sorted(string_var_events, key=lambda item: item[0]):
        if event_kind == "assign":
            path = _clean_path(match.group(2))
            if path and _looks_like_repo_file(path):
                string_vars[match.group(1)] = path
            continue

        path = string_vars.get(match.group(1))
        if not path:
            continue
        mode = match.group(2)
        event_type = "edit" if any(flag in mode for flag in ("w", "a", "+")) else "read"
        positioned_events.append(
            (
                match.start(),
                AgentEvent(event_type=event_type, path=path, command=command),
            )
        )

    for match in re.finditer(
        r"open\(['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]*)['\"]",
        command,
    ):
        path = _clean_path(match.group(1))
        if not path:
            continue
        mode = match.group(2)
        event_type = "edit" if any(flag in mode for flag in ("w", "a", "+")) else "read"
        positioned_events.append(
            (
                match.start(),
                AgentEvent(event_type=event_type, path=path, command=command),
            )
        )
    return [event for _, event in sorted(positioned_events, key=lambda item: item[0])]


def _first_path_token(tokens: list[str]) -> str | None:
    for token in tokens:
        if token.startswith("-"):
            continue
        return _clean_path(token)
    return None


def _clean_path(raw: str) -> str | None:
    path = raw.strip().strip("'\"")
    if not path or path.startswith("-"):
        return None
    for prefix in ("/testbed/", "/app/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _looks_like_repo_file(path: str) -> bool:
    if path.startswith("/"):
        return path.startswith("/testbed/")
    if "/" not in path:
        return False
    return "." in path.rsplit("/", 1)[-1]


def _dedupe(events: list[AgentEvent]) -> list[AgentEvent]:
    seen: set[tuple[str, str, int | None, int | None]] = set()
    deduped: list[AgentEvent] = []
    for event in events:
        key = (event.event_type, event.path, event.start_line, event.end_line)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped
