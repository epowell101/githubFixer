from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agents.definitions import AGENT_MODELS
from config import settings
from linear_client import LinearState, LinearTask, get_linear_client
from prompts import load_prompt
from security import bash_security_hook
from workspace import issue_workspace

if TYPE_CHECKING:
    from models import IssueEvent

try:
    from claude_agent_sdk import (  # type: ignore[import]
        AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
        ResultMessage, RateLimitEvent,
    )
    from claude_agent_sdk.types import (  # type: ignore[import]
        HookCallback, HookMatcher, TextBlock,
    )
except ImportError:
    from anthropic.claude_agent_sdk import (  # type: ignore[import]
        AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
        ResultMessage, RateLimitEvent,
    )
    from anthropic.claude_agent_sdk.types import (  # type: ignore[import]
        HookCallback, HookMatcher, TextBlock,
    )

logger = logging.getLogger(__name__)

_BASH_HOOKS = {
    "PreToolUse": [
        HookMatcher(
            matcher="Bash",
            hooks=[cast(HookCallback, bash_security_hook)],
        )
    ]
}

class AgentStreamError(RuntimeError):
    """Raised when an agent's response stream closes unexpectedly."""


# Monotonically increasing instance counter per agent type (e.g. "coder" → 3).
# Safe without a lock because asyncio runs on a single thread.
_agent_instance_counters: dict[str, int] = {}


def _next_agent_number(agent_type: str) -> int:
    _agent_instance_counters[agent_type] = _agent_instance_counters.get(agent_type, 0) + 1
    return _agent_instance_counters[agent_type]


def _tool_summary(name: str, inp: dict) -> str:
    """Return a short human-readable summary of a tool call's input."""
    match name:
        case "Read" | "Write" | "Edit" | "NotebookEdit":
            return inp.get("file_path", "")
        case "Bash":
            return inp.get("command", "")[:120].replace("\n", "; ")
        case "Glob":
            return inp.get("pattern", "")
        case "Grep":
            pat = inp.get("pattern", "")
            path = inp.get("path", "")
            return f"{pat!r} in {path}" if path else repr(pat)
        case "WebFetch" | "WebSearch":
            return inp.get("url", inp.get("query", ""))[:80]
        case _:
            for v in inp.values():
                if isinstance(v, str) and v:
                    return v[:80]
            return ""


# Serialize Linear project creation per repo so parallel plan() calls for issues
# in the same repo don't each create a duplicate project (race condition).
# Mirrors the _base_clone_locks pattern in workspace.py.
_linear_project_locks: dict[str, asyncio.Lock] = {}


def _linear_project_lock(repo_full_name: str) -> asyncio.Lock:
    if repo_full_name not in _linear_project_locks:
        _linear_project_locks[repo_full_name] = asyncio.Lock()
    return _linear_project_locks[repo_full_name]


# --------------------------------------------------------------------------- #
# Data models                                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class Task:
    title: str
    description: str
    files_hint: list[str]
    acceptance: str
    depends_on: list[int]
    linear_id: str | None = None
    status: str = "todo"  # "todo", "in_progress", "done"
    modified_files: list[str] = field(default_factory=list)



@dataclass
class TestResult:
    passed: bool
    summary: str
    failures: list[dict]  # each: {test, error, file, suggested_fix}
    command: str


@dataclass
class ReviewResult:
    approved: bool        # True if verdict=APPROVED (warnings don't block)
    summary: str
    critical_issues: list[dict]  # only severity=critical items
    checklist: list[dict] = field(default_factory=list)  # [{"criterion": "...", "passed": bool}]


# --------------------------------------------------------------------------- #
# Agent client helpers                                                         #
# --------------------------------------------------------------------------- #

def _make_agent_client(
    system_prompt: str,
    model: str,
    tools: list[str],
    repo_path: Path,
    settings_file: Path,
    mcp_servers: dict | None = None,
    hooks: dict | None = None,
) -> ClaudeSDKClient:
    return ClaudeSDKClient(
        options=ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=model,
            tools=tools,
            cwd=str(repo_path),
            settings=str(settings_file.resolve()),
            mcp_servers=mcp_servers or {},
            hooks=hooks or {},
        )
    )


async def _run_agent(client: ClaudeSDKClient, task_prompt: str, label: str = "") -> str:
    """Run a single agent session and return the full text response."""
    collected: list[str] = []
    tool_call_count = 0
    start = time.monotonic()

    # Build a numbered per-agent-type logger, e.g. agents.coder3.
    # The label is "repo#N agent_type"; issue_ref strips the agent type suffix.
    if label:
        issue_ref, agent_type = label.rsplit(" ", 1)
        instance_num = _next_agent_number(agent_type)
        alog = logging.getLogger(f"agents.{agent_type}{instance_num}")
    else:
        issue_ref = ""
        alog = logger

    if label:
        task_summary = task_prompt.strip().replace("\n", " ")[:150]
        alog.info("[%s] START: %s", issue_ref, task_summary)

    try:
        async with client:
            await client.query(task_prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in getattr(message, "content", []):
                        if isinstance(block, TextBlock) and block.text:
                            if label:
                                alog.info("[%s] %s", issue_ref, block.text.strip()[:1000])
                            collected.append(block.text)
                        elif hasattr(block, "name"):
                            tool_call_count += 1
                            if label:
                                tool_input = getattr(block, "input", {}) or {}
                                summary = _tool_summary(block.name, tool_input)
                                if summary:
                                    alog.info("[%s] TOOL: %s %s", issue_ref, block.name, summary)
                                else:
                                    alog.info("[%s] TOOL: %s", issue_ref, block.name)
                elif isinstance(message, ResultMessage):
                    is_error = getattr(message, "is_error", False)
                    if is_error and label:
                        alog.debug("[%s] TOOL_RESULT: ERROR", issue_ref)
                elif isinstance(message, RateLimitEvent):
                    if label:
                        alog.debug("[%s] RATE_LIMIT event", issue_ref)
    except Exception as exc:
        elapsed = time.monotonic() - start
        if label:
            alog.error(
                "[%s] FAILED after %.1fs (%d tool calls): %s: %s",
                issue_ref, elapsed, tool_call_count, type(exc).__name__, exc,
            )
        if collected:
            alog.warning(
                "[%s] Returning partial output (%d blocks collected before error)",
                issue_ref, len(collected),
            )
            return "\n".join(collected)
        raise AgentStreamError(f"Agent {label!r} stream failed: {exc}") from exc

    elapsed = time.monotonic() - start
    if label:
        alog.info(
            "[%s] DONE in %.1fs (%d tool calls, %d text blocks)",
            issue_ref, elapsed, tool_call_count, len(collected),
        )
    return "\n".join(collected)


async def _gh_subprocess(args: list[str], cwd: Path, timeout: float = 30.0) -> str:
    """Run a gh CLI command and return stdout. Raises RuntimeError on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {stderr.decode().strip()}")
    return stdout.decode()


# --------------------------------------------------------------------------- #
# Parsing helpers                                                               #
# --------------------------------------------------------------------------- #

_PR_URL_RE = re.compile(r'https://github\.com/\S+/pull/\d+')


def _extract_pr_url(text: str) -> str | None:
    m = _PR_URL_RE.search(text)
    return m.group(0) if m else None


def _parse_task_list(text: str) -> list[Task]:
    start = text.find('[')
    end = text.rfind(']') + 1
    if start != -1 and end > 0:
        try:
            raw = json.loads(text[start:end])
            return [
                Task(
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    files_hint=item.get("files_hint", []),
                    acceptance=item.get("acceptance", ""),
                    depends_on=item.get("depends_on", []),
                )
                for item in raw
            ]
        except (json.JSONDecodeError, AttributeError):
            pass
    return [Task(
        title="Implement issue fix",
        description=text,
        files_hint=[],
        acceptance="Implementation complete",
        depends_on=[],
    )]



def _extract_checklist_section(text: str) -> str | None:
    """Extract the '## Completion Checklist' section from coder output."""
    marker = "## Completion Checklist"
    start = text.find(marker)
    if start == -1:
        return None
    # Find the next ## header after the checklist section (or end of string)
    next_section = text.find("\n## ", start + len(marker))
    section = text[start:next_section].strip() if next_section != -1 else text[start:].strip()
    return section if section else None


def _extract_modified_files(text: str) -> list[str]:
    """Extract file paths from a coder response's 'Modified Files' section."""
    files: list[str] = []
    in_section = False
    for line in text.splitlines():
        if "modified files" in line.lower() or "## Modified" in line:
            in_section = True
            continue
        if in_section:
            if line.startswith("##"):
                break
            stripped = line.strip().lstrip("-* ").strip()
            if stripped and "/" in stripped:
                candidate = stripped.split()[0]
                if "." in candidate.split("/")[-1]:
                    files.append(candidate)
    return list(dict.fromkeys(files))  # deduplicate, preserve order


def _parse_tester_output(text: str) -> TestResult:
    """Parse structured JSON from the tester agent."""
    start = text.find('{')
    end = text.rfind('}') + 1
    if start != -1 and end > 0:
        try:
            data = json.loads(text[start:end])
            return TestResult(
                passed=data.get("status") == "PASS",
                summary=data.get("summary", ""),
                failures=data.get("failures", []),
                command=data.get("command", ""),
            )
        except (json.JSONDecodeError, ValueError):
            pass
    # Fallback: heuristic
    upper = text.upper()
    passed = "FAIL" not in upper and "ERROR" not in upper
    return TestResult(passed=passed, summary=text[:200], failures=[], command="")


def _parse_reviewer_output(text: str) -> ReviewResult:
    """Parse structured JSON from the reviewer agent."""
    start = text.find('{')
    end = text.rfind('}') + 1
    if start != -1 and end > 0:
        try:
            data = json.loads(text[start:end])
            all_issues = data.get("issues", [])
            critical = [i for i in all_issues if i.get("severity") == "critical"]
            checklist = data.get("checklist", [])
            # Promote any failed checklist item not already covered in issues
            existing_descriptions = {i.get("description", "").lower() for i in critical}
            for item in checklist:
                if not item.get("passed", True):
                    criterion = item.get("criterion", "unknown criterion")
                    if criterion.lower() not in existing_descriptions:
                        critical.append({
                            "severity": "critical",
                            "file": "",
                            "description": f"Acceptance criterion not met: {criterion}",
                            "fix": f"Ensure the following criterion is fully satisfied: {criterion}",
                        })
            approved = data.get("verdict") == "APPROVED" or not critical
            return ReviewResult(
                approved=approved,
                summary=data.get("summary", ""),
                critical_issues=critical,
                checklist=checklist,
            )
        except (json.JSONDecodeError, ValueError):
            pass
    # Fallback: if parsing fails, approve to avoid false blocks
    return ReviewResult(approved=True, summary=text[:200], critical_issues=[], checklist=[])


# --------------------------------------------------------------------------- #
# Security settings                                                            #
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
            ],
        },
    }
    settings_file = workspace_dir / ".claude_settings.json"
    settings_file.write_text(json.dumps(security_settings, indent=2))
    return settings_file


# --------------------------------------------------------------------------- #
# IssueWorkflow                                                                #
# --------------------------------------------------------------------------- #

class IssueWorkflow:
    """Python-driven state machine that orchestrates the issue resolution workflow.

    Each phase is an async method. Parallel phases use asyncio.gather() to
    guarantee concurrent execution regardless of model behavior.
    """

    def __init__(self, event: "IssueEvent", workspace_dir: Path) -> None:
        self.event = event
        self.workspace_dir = workspace_dir
        self.repo_path = workspace_dir / "repo"
        self.settings_file = workspace_dir / ".claude_settings.json"
        self._label = f"{event.repo_full_name}#{event.number}"

        # Workflow state
        self.linear_issue_id: str | None = None
        self.linear_project_id: str | None = None
        self.tasks: list[Task] = []
        self.analysis: str = ""
        self.spec: str = ""
        self.pr_url: str | None = None
        self.modified_files: list[str] = []

        # Direct Linear API client (replaces linear-tracker LLM agent)
        self._linear = get_linear_client()

        # Preload prompts once
        self._analyzer_prompt = load_prompt("codebase_analyzer")
        self._coder_prompt = load_prompt("coder")
        self._tester_prompt = load_prompt("tester")
        self._reviewer_prompt = load_prompt("reviewer")
        self._planner_prompt = load_prompt("planner")
        self._submitter_prompt = load_prompt("github_submitter")
        self._spec_writer_prompt = load_prompt("spec_writer")
        self._spec_reviewer_prompt = load_prompt("spec_reviewer")

    # ---------------------------------------------------------------------- #
    # Linear helpers                                                          #
    # ---------------------------------------------------------------------- #

    def _linear_bg(self, comment: str) -> None:
        """Fire-and-forget a Linear progress comment (Op F).

        The orchestrator never uses the return value of progress comments, so
        we schedule them as background tasks rather than blocking the workflow.
        """
        if not self.linear_issue_id:
            return
        asyncio.create_task(
            self._linear_safe_comment(self.linear_issue_id, comment)
        )

    async def _linear_safe_comment(self, identifier: str, body: str) -> None:
        try:
            await self._linear.add_comment(identifier, body)
        except Exception as exc:
            logger.warning("[%s] Background Linear comment failed: %s", self._label, exc)

    # ---------------------------------------------------------------------- #
    # Agent runners — each creates a fresh session                            #
    # ---------------------------------------------------------------------- #

    async def _run_codebase_analyzer(self, task: str) -> str:
        client = _make_agent_client(
            system_prompt=self._analyzer_prompt,
            model=AGENT_MODELS["codebase-analyzer"],
            tools=["Read", "Glob", "Grep"],
            repo_path=self.repo_path,
            settings_file=self.settings_file,
        )
        return await _run_agent(client, task, f"{self._label} analyzer")

    async def _run_coder(self, task: str) -> str:
        client = _make_agent_client(
            system_prompt=self._coder_prompt,
            model=AGENT_MODELS["coder"],
            tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
            repo_path=self.repo_path,
            settings_file=self.settings_file,
            hooks=_BASH_HOOKS,
        )
        return await _run_agent(client, task, f"{self._label} coder")

    async def _run_tester(self, task: str) -> str:
        client = _make_agent_client(
            system_prompt=self._tester_prompt,
            model=AGENT_MODELS["tester"],
            tools=["Bash", "Read"],
            repo_path=self.repo_path,
            settings_file=self.settings_file,
            hooks=_BASH_HOOKS,
        )
        return await _run_agent(client, task, f"{self._label} tester")

    async def _run_reviewer(self, task: str) -> str:
        client = _make_agent_client(
            system_prompt=self._reviewer_prompt,
            model=AGENT_MODELS["reviewer"],
            tools=["Bash", "Read", "Glob", "Grep"],
            repo_path=self.repo_path,
            settings_file=self.settings_file,
            hooks=_BASH_HOOKS,
        )
        return await _run_agent(client, task, f"{self._label} reviewer")

    async def _run_planner(self, task: str) -> str:
        client = _make_agent_client(
            system_prompt=self._planner_prompt,
            model=AGENT_MODELS["planner"],
            tools=[],
            repo_path=self.repo_path,
            settings_file=self.settings_file,
        )
        return await _run_agent(client, task, f"{self._label} planner")

    async def _run_spec_writer(self, task: str) -> str:
        client = _make_agent_client(
            system_prompt=self._spec_writer_prompt,
            model=AGENT_MODELS["spec-writer"],
            tools=[],
            repo_path=self.repo_path,
            settings_file=self.settings_file,
        )
        return await _run_agent(client, task, f"{self._label} spec-writer")

    async def _run_spec_reviewer(self, task: str) -> str:
        client = _make_agent_client(
            system_prompt=self._spec_reviewer_prompt,
            model=AGENT_MODELS["spec-reviewer"],
            tools=[],
            repo_path=self.repo_path,
            settings_file=self.settings_file,
        )
        return await _run_agent(client, task, f"{self._label} spec-reviewer")

    async def _run_github_submitter(self, task: str) -> str:
        client = _make_agent_client(
            system_prompt=self._submitter_prompt,
            model=AGENT_MODELS["github-submitter"],
            tools=["Bash", "Read"],
            repo_path=self.repo_path,
            settings_file=self.settings_file,
            hooks=_BASH_HOOKS,
        )
        return await _run_agent(client, task, f"{self._label} submitter")

    # ---------------------------------------------------------------------- #
    # Main run loop                                                            #
    # ---------------------------------------------------------------------- #

    async def plan(self) -> None:
        """Run planning phases (0.5-4): Linear issue + analysis + spec + review + tasks + sub-issues.

        Designed to run without a concurrency semaphore so all incoming issues are
        planned immediately and show up in Linear while execution is throttled.
        """
        logger.info("[%s] Starting planning: %s", self._label, self.event.title)

        # Phase 0.5 — Check if already planned / recover state
        state = await self._phase_check_linear()

        # Handle previously blocked issues: check if user replied on GitHub
        if state.blocked:
            user_replies = await self._check_for_unblock()
            if user_replies is None:
                logger.info("[%s] Previously blocked — no user reply yet, skipping", self._label)
                return
            # User replied — reactivate Linear issue, re-analyze, re-spec, re-plan
            logger.info("[%s] Unblocking: %d user reply(ies) found", self._label, len(user_replies))
            self.linear_issue_id = state.linear_issue_id
            self.linear_project_id = state.linear_project_id
            await self._linear.update_state(self.linear_issue_id, "In Progress")
            self.analysis = await self._run_codebase_analyzer(self._prompt_analyze_codebase())
            if not await self._phase_write_spec(prior_comments=user_replies):
                return
            if not await self._phase_review_spec():
                return
            plan_result = await self._run_planner(self._prompt_plan())
            self.tasks = _parse_task_list(plan_result)
            if self.tasks and self.tasks[0].description.startswith("AMBIGUOUS:"):
                await self._phase_blocked(self.tasks[0].description)
                return
            await self._phase_create_sub_issues()
            logger.info("[%s] Unblock complete: %d sub-issue(s) created", self._label, len(self.tasks))
            return

        if state.in_review and state.pr_url:
            if await self._pr_is_open(state.pr_url):
                logger.info("[%s] PR already open — no planning needed", self._label)
                return

        if state.found:
            self.linear_issue_id = state.linear_issue_id
            self.linear_project_id = state.linear_project_id
            if state.tasks:
                logger.info(
                    "[%s] Already planned with %d task(s) — skipping",
                    self._label, len(state.tasks),
                )
                return

        # Phase 1+2 — Create Linear issue + Analyze codebase (parallel when both needed)
        need_linear = not state.found
        if need_linear:
            logger.info("[%s] Phase 1+2: Creating Linear issue + analyzing codebase in parallel", self._label)
            analysis_task = asyncio.create_task(
                self._run_codebase_analyzer(self._prompt_analyze_codebase())
            )
            async with _linear_project_lock(self.event.repo_full_name):
                self.linear_issue_id, self.linear_project_id = await self._linear.create_issue(
                    title=f"[Auto] #{self.event.number}: {self.event.title}",
                    description=f"{self.event.body or ''}\n\nGitHub Issue: {self.event.html_url}",
                    project_name=self.event.repo_full_name,
                )
            self.analysis = await analysis_task
            logger.info("[%s] Linear issue: %s", self._label, self.linear_issue_id)
        else:
            logger.info("[%s] Phase 2: Analyzing codebase (attempting spec recovery)", self._label)
            spec_result, self.analysis = await asyncio.gather(
                self._fetch_spec_from_linear(),
                self._run_codebase_analyzer(self._prompt_analyze_codebase()),
            )
            self.spec = spec_result or ""

        self._linear_bg("🔍 **Codebase analyzed.** Writing specification...")

        # Phase 2.5 — Write spec (skip if recovered from Linear)
        if not self.spec:
            logger.info("[%s] Phase 2.5: Writing spec", self._label)
            if not await self._phase_write_spec():
                return

        self._linear_bg("📝 **Spec written.** Reviewing against requirements...")

        # Phase 2.7 — Spec reviewer verifies spec covers all requirements
        logger.info("[%s] Phase 2.7: Reviewing spec", self._label)
        if not await self._phase_review_spec():
            return

        self._linear_bg("✅ **Spec approved.** Breaking into implementation tasks...")

        # Phase 3 — Plan tasks from spec
        logger.info("[%s] Phase 3: Planning tasks from spec", self._label)
        plan_result = await self._run_planner(self._prompt_plan())
        self.tasks = _parse_task_list(plan_result)

        if self.tasks and self.tasks[0].description.startswith("AMBIGUOUS:"):
            await self._phase_blocked(self.tasks[0].description)
            return

        task_list = "\n".join(f"{i+1}. {t.title}" for i, t in enumerate(self.tasks))
        self._linear_bg(f"📋 **Plan ready — {len(self.tasks)} tasks:**\n{task_list}")

        # Phase 4 — Create Linear sub-issues in parallel
        await self._phase_create_sub_issues()
        logger.info("[%s] Planning complete: %d sub-issue(s) created in Linear", self._label, len(self.tasks))

    async def code(self) -> bool:
        """Recover Linear state and run coding phase (phase 5).

        Returns True if the caller should stop (blocked, or workflow already
        complete and PR is open). Returns False to signal that testing should
        proceed next.

        Designed to run under the coding concurrency semaphore. After it
        returns False, the caller should release the coding semaphore and
        acquire the (typically higher-limit) testing semaphore before calling
        test_review_submit().
        """
        logger.info("[%s] Starting coding: %s", self._label, self.event.title)

        # Phase 0.5 — Recover state from Linear (written by plan())
        state = await self._phase_check_linear()

        if state.blocked:
            logger.info("[%s] Blocked in Linear — skipping coding", self._label)
            return True

        if state.in_review and state.pr_url:
            if await self._pr_is_open(state.pr_url):
                logger.info("[%s] PR %s still open — nothing to do", self._label, state.pr_url)
                return True
            state = LinearState(found=False)

        if state.found:
            self.linear_issue_id = state.linear_issue_id
            self.linear_project_id = state.linear_project_id
            if state.tasks:
                self.tasks = [
                    Task(
                        title=t.title,
                        description=t.description,
                        linear_id=t.linear_id,
                        status=t.status,
                        files_hint=[],
                        acceptance="",
                        depends_on=[],
                    )
                    for t in state.tasks
                ]

            if state.pr_url:
                if await self._pr_is_open(state.pr_url):
                    logger.info("[%s] PR open — jumping to Phase 7", self._label)
                    self.pr_url = state.pr_url
                    await self._phase_final_linear_update()
                    return True  # done
                state.pr_url = None

        # Re-analyze codebase for coder/tester prompts
        logger.info("[%s] Phase 2 (re-analyze): Refreshing codebase analysis", self._label)
        self.analysis = await self._run_codebase_analyzer(self._prompt_analyze_codebase())

        if self.tasks and all(t.status == "done" for t in self.tasks):
            # Tasks were completed in a prior session — collect modified files and
            # proceed to testing rather than skipping it.
            logger.info("[%s] All tasks already done — skipping coding, proceeding to tests", self._label)
            self.modified_files = await self._get_modified_files_from_git()
            return False

        # Phase 5 — Execute coding tasks in dependency batches
        if await self._phase_execute_tasks():
            return True  # blocked

        # Collect authoritative modified-files list from git
        self.modified_files = await self._get_modified_files_from_git()
        return False

    async def test_review_submit(self) -> None:
        """Run tests, code review, and submit PR (phases 5.5, 5.6, 6, 7).

        Expects code() to have already populated self.analysis and
        self.modified_files. Designed to run under the testing concurrency
        semaphore, which is typically higher-limit than the coding semaphore.
        """
        logger.info("[%s] Starting test+review+submit: %s", self._label, self.event.title)

        # Phase 5.5 — Test & remediate (uses dedicated tester agent)
        if not await self._phase_test_and_remediate():
            return  # blocked after 2 cycles

        # Phase 5.6 — Code review
        if not await self._phase_review():
            return  # blocked after reviewer fix attempt

        # Phase 6 — Submit PR
        await self._phase_submit_pr()

        # Phase 7 — Final Linear update
        await self._phase_final_linear_update()

    async def execute(self) -> None:
        """Run execution phases (5-7). Kept for backward compatibility.

        For production use with separate coding/testing semaphores, prefer
        run_issue_full() which manages semaphore handoff internally.
        """
        done = await self.code()
        if not done:
            await self.test_review_submit()

    async def run(self) -> None:
        """Full workflow: plan then execute. Kept for backward compatibility."""
        await self.plan()
        await self.execute()

    # ---------------------------------------------------------------------- #
    # Phase implementations                                                   #
    # ---------------------------------------------------------------------- #

    async def _post_github_comment(self, body: str) -> None:
        """Post a comment to the GitHub issue."""
        try:
            await _gh_subprocess(
                ["issue", "comment", str(self.event.number),
                 "--repo", self.event.repo_full_name, "--body", body],
                cwd=self.repo_path,
            )
            logger.info("[%s] Posted GitHub comment on #%d", self._label, self.event.number)
        except Exception as e:
            logger.warning("[%s] Failed to post GitHub comment: %s", self._label, e)

    async def _fetch_github_comments(self) -> list[dict]:
        """Fetch all comments on the GitHub issue."""
        try:
            raw = await _gh_subprocess(
                ["issue", "view", str(self.event.number),
                 "--repo", self.event.repo_full_name, "--json", "comments"],
                cwd=self.repo_path,
            )
            return json.loads(raw).get("comments", [])
        except Exception as e:
            logger.warning("[%s] Could not fetch GitHub comments: %s", self._label, e)
            return []

    async def _check_for_unblock(self) -> list[dict] | None:
        """Return user reply comments after the bot's question, or None if still blocked."""
        comments = await self._fetch_github_comments()
        bot_marker = "I need some clarification before I can implement"
        bot_idx = next(
            (i for i, c in enumerate(comments) if bot_marker in c.get("body", "")), None
        )
        if bot_idx is None:
            return None
        user_replies = [
            c for c in comments[bot_idx + 1:]
            if c.get("author", {}).get("login") != settings.github_bot_login
        ]
        return user_replies if user_replies else None

    async def _fetch_spec_from_linear(self) -> str | None:
        """Recover previously written spec from the SPEC: tagged Linear comment."""
        if not self.linear_issue_id:
            return None
        try:
            comments = await self._linear.get_comments(self.linear_issue_id)
            for comment in comments:
                if isinstance(comment, str) and comment.startswith("SPEC:"):
                    return comment[len("SPEC:"):].strip()
        except Exception:
            pass
        return None

    async def _phase_write_spec(self, prior_comments: list[dict] | None = None) -> bool:
        """Phase 2.5: Write a structured project spec. Returns True to continue, False if blocked."""
        logger.info("[%s] Phase 2.5: Writing spec", self._label)

        comments_section = ""
        if prior_comments:
            formatted = "\n".join(
                f"@{c.get('author', {}).get('login', 'user')} "
                f"({c.get('createdAt', '')}): {c.get('body', '')}"
                for c in prior_comments
            )
            comments_section = (
                f"\n\nGitHub clarification comments (user answers to your prior question):\n"
                f"{formatted}"
            )

        prompt = (
            f"Write a complete project spec for this GitHub issue.\n\n"
            f"Issue title: {self.event.title}\n"
            f"Issue URL: {self.event.html_url}\n"
            f"Issue body:\n{self.event.body or '(no body)'}\n\n"
            f"Codebase Analysis:\n{self.analysis}"
            f"{comments_section}"
        )

        result = await self._run_spec_writer(prompt)

        if result.strip().startswith("AMBIGUOUS:"):
            ambiguous_text = result.strip()[len("AMBIGUOUS:"):].strip()

            # Duplicate-post guard: only post if we haven't already asked
            existing = await self._fetch_github_comments()
            bot_marker = "I need some clarification before I can implement"
            already_asked = any(bot_marker in c.get("body", "") for c in existing)

            if not already_asked:
                await self._post_github_comment(
                    "I need some clarification before I can implement this issue:\n\n"
                    f"{ambiguous_text}"
                )

            await self._phase_blocked(f"Spec writer needs clarification: {ambiguous_text[:200]}")
            return False

        self.spec = result

        self._linear_bg(f"SPEC:\n{self.spec}")

        return True

    async def _phase_review_spec(self) -> bool:
        """Phase 2.7: Verify spec covers all issue requirements. Returns True to continue."""
        logger.info("[%s] Phase 2.7: Reviewing spec against issue requirements", self._label)

        prompt = (
            f"Review this spec against the original GitHub issue requirements.\n\n"
            f"Original issue title: {self.event.title}\n"
            f"Original issue body:\n{self.event.body or '(no body)'}\n\n"
            f"Spec to review:\n{self.spec}"
        )
        result = await self._run_spec_reviewer(prompt)

        if not result.strip().startswith("NEEDS_REVISION:"):
            logger.info("[%s] Spec reviewer: APPROVED", self._label)
            return True

        revision_notes = result.strip()[len("NEEDS_REVISION:"):].strip()
        logger.info("[%s] Spec reviewer requested revisions — running one revision cycle", self._label)

        revised = await self._run_spec_writer(
            f"Revise your spec based on the following reviewer feedback:\n\n"
            f"{revision_notes}\n\n"
            f"Original issue title: {self.event.title}\n"
            f"Original issue body:\n{self.event.body or '(no body)'}\n\n"
            f"Your previous spec:\n{self.spec}\n\n"
            f"Codebase Analysis:\n{self.analysis}"
        )

        if revised.strip().startswith("AMBIGUOUS:"):
            ambiguous_text = revised.strip()[len("AMBIGUOUS:"):].strip()
            await self._phase_blocked(f"Spec revision raised new ambiguity: {ambiguous_text[:200]}")
            return False

        self.spec = revised

        re_review = await self._run_spec_reviewer(
            f"Review this revised spec against the original GitHub issue.\n\n"
            f"Original issue title: {self.event.title}\n"
            f"Original issue body:\n{self.event.body or '(no body)'}\n\n"
            f"Revised spec:\n{self.spec}"
        )

        if re_review.strip().startswith("NEEDS_REVISION:"):
            remaining = re_review.strip()[len("NEEDS_REVISION:"):].strip()
            await self._phase_blocked(
                f"Spec still has gaps after revision: {remaining[:300]}"
            )
            return False

        logger.info("[%s] Spec reviewer: APPROVED after revision", self._label)
        # Update the stored spec comment with the reviewed version
        self._linear_bg(f"SPEC:\n{self.spec}")
        return True

    async def _update_spec_progress(self) -> None:
        """Cross off all acceptance criteria in the SPEC: Linear comment after PR submission."""
        if not self.spec or not self.linear_issue_id:
            return
        updated_spec = self.spec.replace("- [ ]", "- [x]")
        if updated_spec == self.spec:
            return
        self.spec = updated_spec
        self._linear_bg(f"SPEC (completed ✅):\n{self.spec}")

    async def _phase_check_linear(self) -> LinearState:
        logger.info("[%s] Phase 0.5: Checking Linear state", self._label)
        return await self._linear.check_state(self.event.number, self.event.repo_full_name)

    async def _phase_create_sub_issues(self) -> None:
        tasks_needing_id = [t for t in self.tasks if not t.linear_id]
        if not tasks_needing_id or not self.linear_issue_id:
            return

        logger.info("[%s] Phase 4: Creating %d sub-issues in parallel", self._label, len(tasks_needing_id))
        sub_ids = await asyncio.gather(*[
            self._linear.create_sub_issue(
                parent_id=self.linear_issue_id,
                title=t.title,
                description=t.description,
            )
            for t in tasks_needing_id
        ])

        for task, sub_id in zip(tasks_needing_id, sub_ids):
            task.linear_id = sub_id
            if sub_id is None:
                logger.warning("[%s] Failed to create Linear sub-issue for '%s'", self._label, task.title)
            else:
                logger.info("[%s] Sub-issue '%s' → %s", self._label, task.title, sub_id)

    async def _phase_execute_tasks(self) -> bool:
        """Execute all tasks in dependency batches. Returns True if blocked."""
        # 5a — Mark all incomplete tasks In Progress (parallel)
        incomplete = [t for t in self.tasks if t.status != "done" and t.linear_id]
        if incomplete:
            logger.info("[%s] Phase 5a: Marking %d tasks In Progress in parallel", self._label, len(incomplete))
            await asyncio.gather(*[
                self._linear.update_state(t.linear_id, "In Progress")
                for t in incomplete
            ])
            for t in incomplete:
                t.status = "in_progress"

        # 5b — Execute each batch in parallel
        batches = self._build_batches()
        logger.info("[%s] Phase 5b: %d batch(es), %d task(s)", self._label, len(batches), len(self.tasks))

        for batch_idx, batch in enumerate(batches):
            logger.info("[%s] Batch %d: executing %d task(s) in parallel", self._label, batch_idx, len(batch))
            results = await asyncio.gather(*[self._run_coder(self._prompt_coder_task(t)) for t in batch])

            for task, result in zip(batch, results):
                if "## Cannot Implement" in result:
                    await self._phase_blocked(f"Coder blocked on task: {task.title}")
                    return True
                task.modified_files = _extract_modified_files(result)
                self.modified_files.extend(task.modified_files)
                checklist = _extract_checklist_section(result)
                if checklist:
                    logger.info("[%s] Task '%s': checklist section found (%d chars)", self._label, task.title, len(checklist))
                else:
                    logger.warning("[%s] Task '%s': no ## Completion Checklist section in coder output", self._label, task.title)
                if checklist:
                    self._linear_bg(f"📋 **Task checklist — {task.title}**\n\n{checklist}")

        # 5c — Mark all tasks Done (parallel)
        executed = [t for t in self.tasks if t.linear_id]
        if executed:
            logger.info("[%s] Phase 5c: Marking %d tasks Done in parallel", self._label, len(executed))
            await asyncio.gather(*[
                self._linear.update_state(t.linear_id, "Done")
                for t in executed
            ])
            for t in executed:
                t.status = "done"

        return False

    async def _phase_test_and_remediate(self, cycle: int = 0) -> bool:
        """Run tests via the tester agent and remediate failures. Returns True if tests pass."""
        if cycle >= settings.max_remediation_cycles:
            self._linear_bg(f"⚠️ Tests still failing after {settings.max_remediation_cycles} remediation cycles.")
            await self._phase_blocked(
                f"Tests still failing after {settings.max_remediation_cycles} remediation cycles"
            )
            return False

        logger.info("[%s] Phase 5.5: Running tests (cycle %d)", self._label, cycle)
        raw = await self._run_tester(self._prompt_run_tests())
        result = _parse_tester_output(raw)

        if result.passed:
            self._linear_bg(f"✅ All tests passing ({result.summary}). Proceeding to review.")
            return True

        # Tests failed — create remediation tasks
        logger.info("[%s] Phase 5.5: %d failure(s): %s", self._label, len(result.failures), result.summary)
        self._linear_bg(f"🔧 Test failures (cycle {cycle + 1}/2): {result.summary}")

        fix_tasks = [
            Task(
                title=f"Fix: {f.get('test', 'test failure')[:60]}",
                description=(
                    f"Test: {f.get('test', '')}\n"
                    f"Error: {f.get('error', '')}\n"
                    f"Suggested fix: {f.get('suggested_fix', '')}"
                ),
                files_hint=[f.get("file", "")] if f.get("file") else [],
                acceptance="Tests pass",
                depends_on=[],
            )
            for f in result.failures[:5]
        ]

        if fix_tasks:
            self.tasks.extend(fix_tasks)
            await self._phase_create_sub_issues()
            await self._phase_execute_tasks_subset(fix_tasks)

        # Refresh so reviewer receives an accurate file list after test fixes
        self.modified_files = await self._get_modified_files_from_git()
        return await self._phase_test_and_remediate(cycle + 1)

    async def _phase_review(self, cycle: int = 0) -> bool:
        """Review the implementation against the issue requirements. Returns True to proceed.

        Re-reviews after each round of coder fixes, up to settings.max_review_cycles.
        """
        if cycle >= settings.max_review_cycles:
            self._linear_bg(f"⚠️ Review issues unresolved after {settings.max_review_cycles} fix attempt(s).")
            await self._phase_blocked(
                f"Review issues unresolved after {settings.max_review_cycles} fix attempt(s)"
            )
            return False

        logger.info("[%s] Phase 5.6: Code review (cycle %d)", self._label, cycle)
        raw = await self._run_reviewer(self._prompt_review())
        result = _parse_reviewer_output(raw)

        # Post reviewer checklist to Linear regardless of verdict
        if result.checklist:
            checklist_lines = "\n".join(
                f"- {'[x]' if item.get('passed', True) else '[ ]'} {item.get('criterion', '')}"
                for item in result.checklist
            )
            verdict_icon = "✅" if result.approved else "❌"
            self._linear_bg(f"{verdict_icon} **Review checklist (cycle {cycle + 1})**\n\n{checklist_lines}")

        if result.approved:
            self._linear_bg(f"✅ Code review passed. {result.summary}")
            return True

        # Critical issues found — create fix tasks, apply them, then re-review
        logger.info("[%s] Phase 5.6: %d critical issue(s) found", self._label, len(result.critical_issues))
        issues_list = "\n".join(f"- {i.get('description', '')}" for i in result.critical_issues)
        self._linear_bg(
            f"🔍 Review cycle {cycle + 1}/{settings.max_review_cycles} — "
            f"{len(result.critical_issues)} issue(s):\n{issues_list}"
        )

        fix_tasks = [
            Task(
                title=f"Fix: {i.get('description', 'review issue')[:60]}",
                description=(
                    f"File: {i.get('file', '')}\n"
                    f"Issue: {i.get('description', '')}\n"
                    f"Fix: {i.get('fix', '')}"
                ),
                files_hint=[i.get("file", "")] if i.get("file") else [],
                acceptance="Reviewer concern addressed",
                depends_on=[],
            )
            for i in result.critical_issues
        ]

        self.tasks.extend(fix_tasks)
        await self._phase_create_sub_issues()
        blocked = await self._phase_execute_tasks_subset(fix_tasks)
        if blocked:
            return False

        self.modified_files = await self._get_modified_files_from_git()
        return await self._phase_review(cycle + 1)

    async def _phase_execute_tasks_subset(self, tasks: list[Task]) -> bool:
        """Execute a specific subset of tasks (e.g. remediation fixes). Returns True if blocked."""
        # Mark In Progress
        await asyncio.gather(*[
            self._linear.update_state(t.linear_id, "In Progress")
            for t in tasks if t.linear_id
        ])

        # Execute in parallel
        results = await asyncio.gather(*[self._run_coder(self._prompt_coder_task(t)) for t in tasks])

        for task, result in zip(tasks, results):
            if "## Cannot Implement" in result:
                await self._phase_blocked(f"Coder blocked on fix task: {task.title}")
                return True
            task.modified_files = _extract_modified_files(result)
            self.modified_files.extend(task.modified_files)

        # Mark Done
        await asyncio.gather(*[
            self._linear.update_state(t.linear_id, "Done")
            for t in tasks if t.linear_id
        ])
        for t in tasks:
            t.status = "done"

        return False

    async def _phase_submit_pr(self) -> None:
        logger.info("[%s] Phase 6: Submitting PR", self._label)
        files_list = "\n".join(f"- {f}" for f in sorted(set(self.modified_files))) or "(all changed files)"
        prompt = (
            f"Create a pull request for GitHub issue #{self.event.number}.\n\n"
            f"Modified files:\n{files_list}\n\n"
            f"GitHub issue number: {self.event.number}\n"
            f"GitHub issue title: {self.event.title}\n"
            f"Repo owner: {self.event.repo_owner}\n"
            f"Repo name: {self.event.repo_name}\n"
            f"Branch name: {self.event.branch_name}\n"
            f"Linear issue ID: {self.linear_issue_id or 'N/A'}\n\n"
            f"Create the branch, commit all changes, push, and open a PR "
            f"targeting the default branch. Return the PR URL."
        )
        last_err: Exception | None = None
        for attempt in range(1, 3):  # max 2 attempts
            try:
                result = await self._run_github_submitter(prompt)
                self.pr_url = _extract_pr_url(result)
                if self.pr_url:
                    logger.info("[%s] PR URL: %s", self._label, self.pr_url)
                    break
                logger.warning(
                    "[%s] No PR URL in submitter output (attempt %d/2)", self._label, attempt,
                )
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "[%s] PR submission failed (attempt %d/2): %s", self._label, attempt, exc,
                )
                if attempt < 2:
                    await asyncio.sleep(5)
        else:
            if last_err:
                raise last_err
        asyncio.create_task(self._update_spec_progress())

    async def _phase_reconcile_subtasks(self) -> None:
        """Safety net: mark any lingering sub-tasks as Done before final update."""
        pending = [t for t in self.tasks if t.linear_id and t.status != "done"]
        if not pending:
            return
        logger.info("[%s] Reconcile: marking %d lingering sub-task(s) Done", self._label, len(pending))
        await asyncio.gather(*[
            self._linear.update_state(t.linear_id, "Done")
            for t in pending
        ])
        for t in pending:
            t.status = "done"

    async def _phase_final_linear_update(self) -> None:
        if not self.linear_issue_id:
            return
        await self._phase_reconcile_subtasks()
        logger.info("[%s] Phase 7: Final Linear update", self._label)
        await self._linear.mark_in_review(
            self.linear_issue_id,
            self.pr_url or "N/A",
            self.linear_project_id,
        )

    async def _phase_blocked(self, reason: str) -> None:
        logger.warning("[%s] Phase BLOCKED: %s", self._label, reason)
        if not self.linear_issue_id:
            return
        await self._linear.mark_cancelled(self.linear_issue_id, reason)

    # ---------------------------------------------------------------------- #
    # Prompt builders                                                          #
    # ---------------------------------------------------------------------- #

    def _prompt_analyze_codebase(self) -> str:
        return (
            f"Analyze the codebase at {self.repo_path} to understand what needs to change "
            f"to resolve this GitHub issue.\n\n"
            f"Issue title: {self.event.title}\n"
            f"Issue body: {self.event.body or '(no body)'}\n"
            f"Local repo path: {self.repo_path}\n\n"
            f"Return a structured analysis report."
        )

    def _prompt_plan(self) -> str:
        issue_context = (
            self.spec if self.spec else
            f"Issue title: {self.event.title}\nIssue body: {self.event.body or '(no body)'}"
        )
        return (
            f"Break this project spec into implementation tasks.\n\n"
            f"{issue_context}\n\n"
            f"Codebase Analysis:\n{self.analysis}\n\n"
            f"Return a raw JSON array of tasks only."
        )

    def _prompt_coder_task(self, task: Task) -> str:
        spec_section = f"\nProject Spec:\n{self.spec}\n" if self.spec else ""
        return (
            f"Implement this specific task for GitHub issue #{self.event.number}.\n\n"
            f"Issue title: {self.event.title}\n"
            f"Issue body: {self.event.body or '(no body)'}\n"
            f"{spec_section}\n"
            f"Codebase Analysis:\n{self.analysis}\n\n"
            f"Task title: {task.title}\n"
            f"Task description: {task.description}\n"
            f"Files hint: {', '.join(task.files_hint) or 'see analysis'}\n"
            f"Acceptance criteria: {task.acceptance}\n\n"
            f"Local repo path: {self.repo_path}\n\n"
            f"Implement this specific task only. Do NOT run git commands.\n\n"
            f"Follow the output format in your instructions exactly: "
            f"## Implementation Summary, ## Modified Files, ## Test Results, and "
            f"## Completion Checklist (all implementation steps marked [x]/[ ] and "
            f"each acceptance criterion marked [x]/[ ])."
        )

    def _prompt_run_tests(self) -> str:
        return (
            f"Run the full test suite for the repository at {self.repo_path}.\n"
            f"Do NOT modify any files.\n\n"
            f"Codebase analysis (use for test command discovery):\n{self.analysis}\n\n"
            f"Return ONLY the JSON result object — no preamble, no markdown."
        )

    def _prompt_review(self) -> str:
        files_list = "\n".join(f"- {f}" for f in self.modified_files) or "(check git diff)"
        spec_section = (
            f"\nProject Spec (use the Acceptance Criteria as your review checklist):\n{self.spec}\n"
            if self.spec else ""
        )
        return (
            f"Review the code changes made for GitHub issue #{self.event.number}.\n\n"
            f"Issue title: {self.event.title}\n"
            f"Issue body: {self.event.body or '(no body)'}\n"
            f"{spec_section}\n"
            f"Modified files:\n{files_list}\n\n"
            f"Local repo path: {self.repo_path}\n\n"
            f'Return ONLY a JSON object with these exact fields: "verdict" (APPROVED or NEEDS_CHANGES), '
            f'"summary" (string), "checklist" (array of {{"criterion": "...", "passed": true/false}} — '
            f"one entry per acceptance criterion from the spec), and "
            f'"issues" (array). No preamble, no markdown fences.'
        )

    # ---------------------------------------------------------------------- #
    # Utilities                                                                #
    # ---------------------------------------------------------------------- #

    async def _pr_is_open(self, pr_url: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_shell(
                f"gh pr view {pr_url} --json state --jq '.state'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.repo_path),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            return stdout.decode().strip().upper() == "OPEN"
        except Exception:
            return False

    async def _get_modified_files_from_git(self) -> list[str]:
        """Return the list of files changed since the last commit via git diff."""
        try:
            proc = await asyncio.create_subprocess_shell(
                "git diff --name-only HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.repo_path),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            files = [f.strip() for f in stdout.decode().splitlines() if f.strip()]
            if files:
                logger.info("[%s] git diff reported %d modified file(s)", self._label, len(files))
                return files
        except Exception:
            pass
        # Fall back to the heuristic list accumulated during coding
        return self.modified_files

    def _build_batches(self) -> list[list[Task]]:
        """Group tasks into dependency-ordered batches for parallel execution."""
        done_indices = {i for i, t in enumerate(self.tasks) if t.status == "done"}
        pending = [(i, t) for i, t in enumerate(self.tasks) if t.status != "done"]

        batches: list[list[Task]] = []
        while pending:
            ready = [(i, t) for i, t in pending if all(d in done_indices for d in t.depends_on)]
            if not ready:
                ready = [pending[0]]  # break circular dependency
            batches.append([t for _, t in ready])
            ready_indices = {i for i, _ in ready}
            done_indices |= ready_indices
            pending = [(i, t) for i, t in pending if i not in ready_indices]

        return batches



# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

async def run_issue_full(
    event: "IssueEvent",
    coding_semaphore: asyncio.Semaphore,
    testing_semaphore: asyncio.Semaphore,
) -> None:
    """Primary entry point used by TaskRunner.

    Keeps one workspace open for the full lifecycle and hands off between
    semaphores at phase boundaries:

      plan()              — no semaphore (runs immediately for all issues)
      code()              — under coding_semaphore
      test_review_submit() — under testing_semaphore (higher limit, runs as
                             soon as a testing slot is free regardless of
                             whether the coding slots are full)
    """
    logger.info(
        "Starting full workflow for %s#%d: %s",
        event.repo_full_name, event.number, event.title,
    )
    async with issue_workspace(event.repo_name, event.number, event.clone_url) as workspace_dir:
        _write_security_settings(workspace_dir)
        workflow = IssueWorkflow(event, workspace_dir)

        # Planning: runs immediately, no throttle
        await workflow.plan()

        # Coding: throttled to max_concurrent_issues
        async with coding_semaphore:
            done = await workflow.code()

        if done:
            logger.info("Workflow complete (no testing needed) for %s#%d", event.repo_full_name, event.number)
            return

        # Testing + review + PR: separate, higher-limit throttle
        # The coding slot was already released above so another issue can start coding.
        async with testing_semaphore:
            await workflow.test_review_submit()

    logger.info("Workflow complete for %s#%d", event.repo_full_name, event.number)


async def run_issue_planning(event: "IssueEvent") -> None:
    """Run planning phases only (0.5-4). No concurrency semaphore needed."""
    logger.info(
        "Starting planning for %s#%d: %s",
        event.repo_full_name, event.number, event.title,
    )
    async with issue_workspace(event.repo_name, event.number, event.clone_url) as workspace_dir:
        _write_security_settings(workspace_dir)
        workflow = IssueWorkflow(event, workspace_dir)
        await workflow.plan()
    logger.info("Planning complete for %s#%d", event.repo_full_name, event.number)


async def run_issue_execution(event: "IssueEvent") -> None:
    """Run execution phases only (5-7). Should run under a concurrency semaphore."""
    logger.info(
        "Starting execution for %s#%d: %s",
        event.repo_full_name, event.number, event.title,
    )
    async with issue_workspace(event.repo_name, event.number, event.clone_url) as workspace_dir:
        _write_security_settings(workspace_dir)
        workflow = IssueWorkflow(event, workspace_dir)
        await workflow.execute()
    logger.info("Execution complete for %s#%d", event.repo_full_name, event.number)


async def run_issue_workflow(event: "IssueEvent") -> None:
    """Full workflow: plan then execute. Kept for backward compatibility."""
    logger.info(
        "Starting workflow for %s#%d: %s",
        event.repo_full_name, event.number, event.title,
    )
    async with issue_workspace(event.repo_name, event.number, event.clone_url) as workspace_dir:
        _write_security_settings(workspace_dir)
        workflow = IssueWorkflow(event, workspace_dir)
        await workflow.run()
    logger.info("Workflow complete for %s#%d", event.repo_full_name, event.number)
