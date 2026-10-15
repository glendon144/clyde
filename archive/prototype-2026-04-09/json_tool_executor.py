#!/usr/bin/env python3
"""
json_tool_executor.py

A small, deterministic executor for LLM-emitted tool-call JSON.

Purpose:
- Accept JSON emitted by a local model that *describes* a tool call
- Extract the JSON even if the model wrapped it in explanatory text
- Validate it against a strict schema and command whitelist
- Execute the approved command safely
- Return structured JSON output

Example accepted input:

{
  "name": "bash",
  "arguments": {
    "command": "ls",
    "description": "Lists the files and directories in the current directory."
  }
}

This script is intentionally conservative:
- Only a small whitelist of commands is allowed by default
- Shell metacharacters are rejected
- No shell=True
- No pipelines, redirects, command substitution, etc.

This is a "bright intern with a clipboard" design:
the model suggests, Python decides.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional


ALLOWED_TOOLS = {"bash"}

# Keep this small and boring at first.
ALLOWED_COMMANDS = {
    "ls",
    "pwd",
    "find",
    "grep",
    "cat",
    "head",
    "tail",
    "wc",
    "file",
    "python3",
}

# Reject obvious shell trickery.
FORBIDDEN_SHELL_TOKENS = {
    "|",
    ">",
    ">>",
    "<",
    "&&",
    "||",
    ";",
    "`",
    "$(",
    "${",
}

DEFAULT_TIMEOUT = 10


@dataclass
class ExecResult:
    ok: bool
    tool: Optional[str] = None
    command: Optional[str] = None
    cwd: Optional[str] = None
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    description: Optional[str] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate and execute LLM-emitted tool JSON safely."
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="Raw text containing JSON or surrounding chatter.")
    src.add_argument("--input-file", help="Path to a file containing raw text or JSON.")

    parser.add_argument(
        "--cwd",
        default=".",
        help="Working directory in which to run the command. Default: current directory.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Command timeout in seconds. Default: {DEFAULT_TIMEOUT}",
    )
    parser.add_argument(
        "--pretty", action="store_true", help="Pretty-print JSON output."
    )
    return parser.parse_args()


def load_text(args: argparse.Namespace) -> str:
    if args.input is not None:
        return args.input
    return Path(args.input_file).read_text(encoding="utf-8", errors="replace")


def extract_first_json_object(text: str) -> dict[str, Any]:
    """
    Extract the first balanced JSON object from arbitrary text.

    This is intentionally simple and robust enough for model output like:
    "I'll use bash now.\n{ ...json... }"
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in input.")

    depth = 0
    in_string = False
    escape = False
    end = None

    for i in range(start, len(text)):
        ch = text[i]

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end is None:
        raise ValueError("Found '{' but could not locate a complete JSON object.")

    candidate = text[start:end]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse extracted JSON: {exc}") from exc


def normalize_tool_payload(payload: dict[str, Any]) -> tuple[str, str, Optional[str]]:
    """
    Accept a few minor variants, but keep the schema narrow.

    Supported schema:
    {
      "name": "bash",
      "arguments": {
        "command": "ls",
        "description": "..."
      }
    }

    Also tolerates:
    {
      "tool": "bash",
      "command": "ls"
    }
    """
    if "name" in payload and "arguments" in payload:
        tool = payload["name"]
        arguments = payload["arguments"]
        if not isinstance(arguments, dict):
            raise ValueError("'arguments' must be an object.")
        command = arguments.get("command")
        description = arguments.get("description")
    elif "tool" in payload and "command" in payload:
        tool = payload["tool"]
        command = payload["command"]
        description = payload.get("description")
    else:
        raise ValueError(
            "Unsupported schema. Expected {'name': ..., 'arguments': {...}} "
            "or {'tool': ..., 'command': ...}."
        )

    if not isinstance(tool, str) or not tool.strip():
        raise ValueError("Tool name must be a non-empty string.")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Command must be a non-empty string.")
    if description is not None and not isinstance(description, str):
        raise ValueError("Description must be a string if present.")

    return tool.strip(), command.strip(), description


def ensure_safe_cwd(cwd: str) -> str:
    resolved = str(Path(cwd).expanduser().resolve())
    if not os.path.isdir(resolved):
        raise ValueError(f"Working directory does not exist: {resolved}")
    return resolved


def has_forbidden_shell_syntax(command: str) -> Optional[str]:
    for token in FORBIDDEN_SHELL_TOKENS:
        if token in command:
            return token

    # Also reject newlines to avoid multi-command tricks.
    if "\n" in command or "\r" in command:
        return "newline"

    return None


def validate_command(command: str) -> list[str]:
    bad_token = has_forbidden_shell_syntax(command)
    if bad_token:
        raise ValueError(f"Forbidden shell syntax detected: {bad_token}")

    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError(f"Command could not be parsed: {exc}") from exc

    if not argv:
        raise ValueError("Command is empty after parsing.")

    prog = argv[0]
    if prog not in ALLOWED_COMMANDS:
        raise ValueError(
            f"Command '{prog}' is not allowed. Allowed commands: "
            f"{', '.join(sorted(ALLOWED_COMMANDS))}"
        )

    # Optional extra guardrails for certain commands.
    if prog == "find":
        # Keep it simple for now.
        if len(argv) > 8:
            raise ValueError("find command too long for current safety policy.")
    elif prog == "grep":
        if len(argv) > 10:
            raise ValueError("grep command too long for current safety policy.")
    elif prog == "python3":
        # Allow inline one-liners? No. Safer to restrict to script files.
        if len(argv) >= 2 and argv[1] == "-c":
            raise ValueError("python3 -c is not allowed.")
    return argv


def execute_tool(
    tool: str,
    command: str,
    description: Optional[str],
    cwd: str,
    timeout: int,
) -> ExecResult:
    if tool not in ALLOWED_TOOLS:
        return ExecResult(
            ok=False,
            tool=tool,
            command=command,
            cwd=cwd,
            error=f"Tool '{tool}' is not allowed. Allowed tools: {', '.join(sorted(ALLOWED_TOOLS))}",
            description=description,
        )

    try:
        argv = validate_command(command)
    except ValueError as exc:
        return ExecResult(
            ok=False,
            tool=tool,
            command=command,
            cwd=cwd,
            error=str(exc),
            description=description,
        )

    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return ExecResult(
            ok=(proc.returncode == 0),
            tool=tool,
            command=command,
            cwd=cwd,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            description=description,
        )
    except subprocess.TimeoutExpired as exc:
        return ExecResult(
            ok=False,
            tool=tool,
            command=command,
            cwd=cwd,
            error=f"Command timed out after {timeout} seconds.",
            stdout=(exc.stdout or ""),
            stderr=(exc.stderr or ""),
            description=description,
        )
    except Exception as exc:
        return ExecResult(
            ok=False,
            tool=tool,
            command=command,
            cwd=cwd,
            error=f"Execution error: {exc}",
            description=description,
        )


def main() -> int:
    args = parse_args()

    try:
        raw_text = load_text(args)
        payload = extract_first_json_object(raw_text)
        tool, command, description = normalize_tool_payload(payload)
        cwd = ensure_safe_cwd(args.cwd)
        result = execute_tool(
            tool=tool,
            command=command,
            description=description,
            cwd=cwd,
            timeout=args.timeout,
        )
    except Exception as exc:
        result = ExecResult(ok=False, error=str(exc))

    dump = json.dumps(asdict(result), indent=2 if args.pretty else None)
    print(dump)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
