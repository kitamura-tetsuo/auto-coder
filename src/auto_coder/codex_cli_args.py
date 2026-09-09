"""Shared command assembly for finite-input Codex CLI tasks."""

from __future__ import annotations

from collections.abc import Sequence

_INPUT_OPTION_NAMES = ("--prompt", "--input")
_OPTIONS_WITH_VALUES = {
    "--ask-for-approval",
    "--config",
    "--model",
    "--sandbox",
    "-a",
    "-c",
    "-m",
    "-s",
}


def build_codex_exec_command(
    options: Sequence[str],
    extra_args: Sequence[str],
    *,
    session_id: str | None = None,
) -> list[str]:
    """Build one ``codex exec`` command whose only task input is stdin.

    The configured argument lists are intentionally otherwise preserved.  A
    configured stdin operand (or prompt/input option) is rejected rather than
    allowing it to compete with the caller-owned task payload.
    """
    arguments = [str(value) for value in (*options, *extra_args)]
    if any(argument == "-" or argument in _INPUT_OPTION_NAMES or any(argument.startswith(f"{name}=") for name in _INPUT_OPTION_NAMES) for argument in arguments):
        raise ValueError("Codex options contain a conflicting task-input source")

    exec_indexes = [index for index, argument in enumerate(arguments) if argument == "exec" and (index == 0 or arguments[index - 1] not in _OPTIONS_WITH_VALUES)]
    if len(exec_indexes) > 1:
        raise ValueError("Codex options contain multiple exec subcommands")
    if exec_indexes:
        command = ["codex", *arguments]
        exec_index = exec_indexes[0] + 1
    else:
        command = ["codex", "exec", *arguments]
        exec_index = 1

    if session_id is not None:
        command[exec_index + 1 : exec_index + 1] = ["resume", session_id]
    command.append("-")
    return command
