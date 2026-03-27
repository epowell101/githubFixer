from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from linear_config import get_linear_mcp_config
from agents.definitions import (
    make_codebase_analyzer,
    make_coder,
    make_github_submitter,
    make_linear_tracker,
    make_planner,
)
from config import settings
from prompts import load_prompt
from workspace import issue_workspace

if TYPE_CHECKING:
    from models import IssueEvent

from typing import cast

try:
    from claude_agent_sdk import (  # type: ignore[import]
        AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, RateLimitEvent,
        ResultMessage, UserMessage,
    )
    from claude_agent_sdk.types import (  # type: ignore[import]
        HookCallback, HookMatcher, TextBlock, ToolResultBlock, ToolUseBlock,
    )
except ImportError:
    from anthropic.claude_agent_sdk import (  # type: ignore[import]
        AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, RateLimitEvent,
        ResultMessage, UserMessage,
    )
    from anthropic.claude_agent_sdk.types import (  # type: ignore[import]
        HookCallback, HookMatcher, TextBlock, ToolResultBlock, ToolUseBlock,
    )

from security import bash_security_hook

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Security settings                                                             #
# --------------------------------------------------------------------------- #

def _write_security_settings(workspace_dir: Path) -> Path:
    security_settings = {
        "permissions": {
            "defaultMode": "acceptEdits",
            "allow": [
                "Read(./**)",
                "Write(./**)",
                "Edit(./**)",
                "Glob(./**)",
                "Grep(./**)",
                "Bash(*)",
                "mcp__linear__*",
            ],
        },
    }
    settings_file = workspace_dir / ".claude_settings.json"
    settings_file.write_text(json.dumps(security_settings, indent=2))
    return settings_file


# --------------------------------------------------------------------------- #
# Prompt builder                                                                #
# --------------------------------------------------------------------------- #

def _build_orchestrator_prompt(event: "IssueEvent", workspace_dir: Path) -> str:
    repo_path = workspace_dir / "repo"
    return f"""You are resolving GitHub issue #{event.number} from the repository {event.repo_full_name}.

**Issue Title:** {event.title}

**Issue Body:**
{event.body or "(no body provided)"}

**Repository:** {event.repo_full_name}
**Repo Owner:** {event.repo_owner}
**Repo Name:** {event.repo_name}
**Local Clone Path:** {repo_path}
**Issue URL:** {event.html_url}
**Branch to create:** {event.branch_name}
**Linear Team ID:** {settings.linear_team_id}
**Linear Project Name:** {event.repo_full_name}

Begin with Phase 0.5 to check Linear for existing state, then follow phases in order.
Pass data explicitly between agents.
"""


# --------------------------------------------------------------------------- #
# Client factory                                                                #
# --------------------------------------------------------------------------- #

def _make_client(repo_path: Path, settings_file: Path) -> ClaudeSDKClient:
    return ClaudeSDKClient(
        options=ClaudeAgentOptions(
            system_prompt=load_prompt("orchestrator"),
            model=settings.orchestrator_model,
            cwd=str(repo_path),
            settings=str(settings_file.resolve()),
            mcp_servers=get_linear_mcp_config(),
            agents={
                "codebase-analyzer": make_codebase_analyzer(),
                "coder": make_coder(),
                "github-submitter": make_github_submitter(),
                "linear-tracker": make_linear_tracker(),
                "planner": make_planner(),
            },
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher="Bash",
                        hooks=[cast(HookCallback, bash_security_hook)],
                    ),
                ],
            },
        )
    )


# --------------------------------------------------------------------------- #
# Agent session runner                                                          #
# --------------------------------------------------------------------------- #

async def _run_agent_session(
    client: ClaudeSDKClient,
    prompt: str,
    event: "IssueEvent",
) -> tuple[str, dict | None, float | None, list]:
    """Run one agent session; returns (response_text, usage, cost_usd, rate_limit_events)."""
    collected: list[str] = []
    usage: dict | None = None
    cost_usd: float | None = None
    rate_limit_events: list = []
    async with client:
        await client.query(prompt)
        async for message in client.receive_response():
            _log_message(message, event)
            if isinstance(message, AssistantMessage):
                for block in getattr(message, "content", []):
                    if isinstance(block, TextBlock) and block.text:
                        collected.append(block.text)
            elif isinstance(message, ResultMessage):
                usage = getattr(message, "usage", None)
                cost_usd = getattr(message, "total_cost_usd", None)
            elif isinstance(message, RateLimitEvent):
                rate_limit_events.append(message)
    return "\n".join(collected), usage, cost_usd, rate_limit_events


# --------------------------------------------------------------------------- #
# Main workflow                                                                 #
# --------------------------------------------------------------------------- #

async def run_issue_workflow(event: "IssueEvent") -> None:
    logger.info(
        "Starting workflow for %s#%d: %s",
        event.repo_full_name, event.number, event.title,
    )

    async with issue_workspace(event.repo_name, event.number, event.clone_url) as workspace_dir:
        settings_file = _write_security_settings(workspace_dir)
        repo_path = workspace_dir / "repo"

        client = _make_client(repo_path, settings_file)
        prompt = _build_orchestrator_prompt(event, workspace_dir)

        logger.info("Running orchestrator for %s#%d", event.repo_full_name, event.number)

        response_text, usage, cost_usd, rate_limit_events = await _run_agent_session(
            client, prompt, event
        )

        try:
            from token_tracker import print_usage_summary, record_usage
            issue_ref = f"{event.repo_full_name}#{event.number}"
            record_usage(issue_ref, usage, cost_usd)
            print_usage_summary(
                issue_ref=issue_ref,
                last_usage=usage,
                last_cost=cost_usd,
                rate_limit_events=rate_limit_events,
            )
        except Exception:
            logger.warning("Token tracking failed — continuing", exc_info=True)

    logger.info("Workflow complete for %s#%d", event.repo_full_name, event.number)


# --------------------------------------------------------------------------- #
# Message logging                                                               #
# --------------------------------------------------------------------------- #

def _log_message(message: object, event: "IssueEvent") -> None:
    tag = f"[{event.repo_full_name}#{event.number}]"

    if isinstance(message, AssistantMessage):
        for block in getattr(message, "content", []):
            if isinstance(block, TextBlock) and block.text.strip():
                logger.info("%s %s", tag, block.text.strip()[:300])
            elif isinstance(block, ToolUseBlock):
                inp = getattr(block, "input", {})
                brief = str(inp)[:200] if inp else ""
                logger.info("%s → tool_use: %s  input: %s", tag, block.name, brief)

    elif isinstance(message, UserMessage):
        for block in getattr(message, "content", []):
            if isinstance(block, ToolResultBlock):
                if getattr(block, "is_error", False):
                    logger.warning(
                        "%s ← TOOL ERROR (tool_use_id=%s): %s",
                        tag,
                        getattr(block, "tool_use_id", "?"),
                        str(block.content)[:500],
                    )
                else:
                    content_preview = str(getattr(block, "content", ""))[:150]
                    logger.info("%s ← tool_result OK: %s", tag, content_preview)

    else:
        msg_type = getattr(message, "type", type(message).__name__)
        if msg_type not in ("system", "rate_limit"):
            logger.debug("%s [%s]", tag, msg_type)
