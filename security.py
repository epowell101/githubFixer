"""
Security Hooks for GitHub Issue Solver
=======================================

Pre-tool-use hooks that validate bash commands.
Uses an allowlist approach — only explicitly permitted commands can run.
"""

from __future__ import annotations

import os
import re
import shlex

from typing import Any

from claude_code_sdk.types import HookContext, HookJSONOutput


ALLOWED_BASE_COMMANDS: set[str] = {
    # File inspection
    "ls", "cat", "head", "tail", "wc", "grep", "find",
    # File ops
    "cp", "mv", "mkdir", "rm", "touch",
    # Navigation / output
    "pwd", "cd", "echo", "printf",
    # HTTP
    "curl",
    # Environment
    "which", "env",
    # Python
    "python", "python3", "pytest", "pip", "pip3",
    # Node
    "npm", "npx", "node", "yarn",
    # Version control
    "git",
    # GitHub CLI — needed for PR creation and pushing to private repos
    "gh",
    # Build / language tools
    "make",
    "go", "cargo", "rustc",
    "ruby", "bundle", "rspec",
    "java", "javac", "mvn", "gradle",
}

ALLOWED_GIT_SUBCOMMANDS: set[str] = {
    "status", "diff", "log", "add", "commit", "push", "clone",
    "checkout", "branch", "fetch", "stash", "show",
    "rev-parse", "remote", "config", "worktree",
}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _extract_commands(command_string: str) -> list[str]:
    """Extract base command names from a (possibly compound) shell string."""
    commands: list[str] = []
    segments = re.split(r'(?<!["\'])\s*;\s*(?!["\'])', command_string)

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return []  # malformed → block

        expect_command = True
        for token in tokens:
            if token in ("|", "||", "&&", "&"):
                expect_command = True
                continue
            if token in ("if", "then", "else", "elif", "fi", "for", "while",
                         "until", "do", "done", "case", "esac", "in", "!", "{", "}"):
                continue
            if token.startswith("-"):
                continue
            if "=" in token and not token.startswith("="):
                continue
            if expect_command:
                commands.append(os.path.basename(token))
                expect_command = False

    return commands


def _validate_git_subcommand(command_string: str) -> tuple[bool, str]:
    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return False, "Could not parse git command"
    if len(tokens) < 2:
        return True, ""
    sub = tokens[1].lstrip("-")
    if sub not in ALLOWED_GIT_SUBCOMMANDS:
        return False, f"git subcommand '{sub}' is not allowed"
    return True, ""


def _find_segment(cmd: str, command_string: str) -> str:
    segments = re.split(r"\s*(?:&&|\|\|)\s*", command_string)
    all_segs: list[str] = []
    for seg in segments:
        all_segs.extend(re.split(r'(?<!["\'])\s*;\s*(?!["\'])', seg))
    for seg in all_segs:
        if cmd in _extract_commands(seg):
            return seg.strip()
    return command_string


# ---------------------------------------------------------------------------
# Public validator (used by unit tests and the hook)
# ---------------------------------------------------------------------------

def validate_bash_command(command: str) -> tuple[bool, str]:
    """
    Returns (allowed, reason).  reason is empty when allowed.
    """
    commands = _extract_commands(command)
    if not commands:
        return False, "Could not parse command"

    for cmd in commands:
        if cmd not in ALLOWED_BASE_COMMANDS:
            return False, f"Command '{cmd}' is not in the allowed list"
        if cmd == "git":
            seg = _find_segment("git", command)
            ok, reason = _validate_git_subcommand(seg)
            if not ok:
                return False, reason

    return True, ""


# ---------------------------------------------------------------------------
# Hook — must match the SDK's expected async signature exactly
# ---------------------------------------------------------------------------

async def bash_security_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None = None,
    context: HookContext | None = None,
) -> HookJSONOutput:
    """PreToolUse hook: validate Bash commands against the allowlist."""
    if input_data.get("tool_name") != "Bash":
        return {}

    command: str = input_data.get("tool_input", {}).get("command", "")
    if not command:
        return {}

    allowed, reason = validate_bash_command(command)
    if not allowed:
        return {
            "decision": "block",
            "systemMessage": f"Security policy blocked: {reason}. Command: {command!r}",
        }
    return {}
