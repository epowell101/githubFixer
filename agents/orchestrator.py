from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agents.definitions import AGENT_MODELS
from config import settings
from linear_config import LINEAR_TOOLS, get_linear_mcp_config
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
class LinearState:
    found: bool
    blocked: bool = False
    in_review: bool = False
    pr_url: str | None = None
    linear_issue_id: str | None = None
    linear_project_id: str | None = None
    tasks: list[Task] = field(default_factory=list)


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
    async with client:
        await client.query(task_prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in getattr(message, "content", []):
                    if isinstance(block, TextBlock) and block.text:
                        if label:
                            logger.info("[%s] %s", label, block.text.strip()[:300])
                        collected.append(block.text)
    return "\n".join(collected)


# --------------------------------------------------------------------------- #
# Parsing helpers                                                               #
# --------------------------------------------------------------------------- #

_LINEAR_ID_RE = re.compile(r'\b([A-Z]+-\d+)\b')
_UUID_RE = re.compile(
    r'\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b', re.I
)
_PR_URL_RE = re.compile(r'https://github\.com/\S+/pull/\d+')


def _extract_linear_id(text: str) -> str | None:
    m = _LINEAR_ID_RE.search(text)
    return m.group(1) if m else None


def _extract_uuid(text: str) -> str | None:
    m = _UUID_RE.search(text)
    return m.group(1) if m else None


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


def _parse_linear_state(text: str) -> LinearState:
    start = text.find('{')
    end = text.rfind('}') + 1
    if start == -1 or end == 0:
        return LinearState(found=False)
    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError:
        return LinearState(found=False)

    if not data.get("found"):
        return LinearState(found=False)

    tasks = [
        Task(
            title=t.get("title", ""),
            description=t.get("description", ""),
            files_hint=[],
            acceptance="",
            depends_on=[],
            linear_id=t.get("linear_id"),
            status=t.get("status", "todo"),
        )
        for t in data.get("tasks", [])
    ]
    return LinearState(
        found=True,
        blocked=data.get("blocked", False),
        in_review=data.get("in_review", False),
        pr_url=data.get("pr_url"),
        linear_issue_id=data.get("linear_issue_id"),
        linear_project_id=data.get("linear_project_id"),
        tasks=tasks,
    )


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
            approved = data.get("verdict") == "APPROVED" or not critical
            return ReviewResult(
                approved=approved,
                summary=data.get("summary", ""),
                critical_issues=critical,
            )
        except (json.JSONDecodeError, ValueError):
            pass
    # Fallback: if parsing fails, approve to avoid false blocks
    return ReviewResult(approved=True, summary=text[:200], critical_issues=[])


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
                "mcp__linear__*",
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
        self.pr_url: str | None = None
        self.modified_files: list[str] = []

        # Preload prompts once
        self._linear_prompt = load_prompt("linear_tracker")
        self._analyzer_prompt = load_prompt("codebase_analyzer")
        self._coder_prompt = load_prompt("coder")
        self._tester_prompt = load_prompt("tester")
        self._reviewer_prompt = load_prompt("reviewer")
        self._planner_prompt = load_prompt("planner")
        self._submitter_prompt = load_prompt("github_submitter")

    # ---------------------------------------------------------------------- #
    # Agent runners — each creates a fresh session                            #
    # ---------------------------------------------------------------------- #

    async def _run_linear_tracker(self, task: str) -> str:
        client = _make_agent_client(
            system_prompt=self._linear_prompt,
            model=AGENT_MODELS["linear-tracker"],
            tools=LINEAR_TOOLS,
            repo_path=self.repo_path,
            settings_file=self.settings_file,
            mcp_servers=get_linear_mcp_config(),
        )
        return await _run_agent(client, task, f"{self._label} linear")

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
        """Run planning phases (0.5-4): Linear issue + analysis + task planning + sub-issues.

        Designed to run without a concurrency semaphore so all incoming issues are
        planned immediately and show up in Linear while execution is throttled.
        """
        logger.info("[%s] Starting planning: %s", self._label, self.event.title)

        # Phase 0.5 — Check if already planned
        state = await self._phase_check_linear()

        if state.blocked:
            logger.info("[%s] Previously blocked — skipping planning", self._label)
            return

        if state.in_review and state.pr_url:
            if await self._pr_is_open(state.pr_url):
                logger.info("[%s] PR already open — no planning needed", self._label)
                return

        if state.found:
            self.linear_issue_id = state.linear_issue_id
            self.linear_project_id = state.linear_project_id
            if state.tasks:
                # Already planned and sub-issues created — nothing to do
                logger.info(
                    "[%s] Already planned with %d task(s) — skipping",
                    self._label, len(state.tasks),
                )
                return

        # Phase 1+2 — Create Linear issue + Analyze codebase (parallel when both needed)
        need_linear = not state.found
        if need_linear:
            logger.info("[%s] Phase 1+2: Creating Linear issue + analyzing codebase in parallel", self._label)
            # Codebase analysis runs freely; project creation is serialized per repo
            # to prevent concurrent plan() calls from creating duplicate Linear projects.
            analysis_task = asyncio.create_task(
                self._run_codebase_analyzer(self._prompt_analyze_codebase())
            )
            async with _linear_project_lock(self.event.repo_full_name):
                linear_result = await self._run_linear_tracker(self._prompt_create_linear_issue())
                self.linear_issue_id = _extract_linear_id(linear_result)
                self.linear_project_id = _extract_uuid(linear_result)
            self.analysis = await analysis_task
            logger.info("[%s] Linear issue: %s", self._label, self.linear_issue_id)
        else:
            logger.info("[%s] Phase 2: Analyzing codebase", self._label)
            self.analysis = await self._run_codebase_analyzer(self._prompt_analyze_codebase())

        if self.linear_issue_id:
            await self._run_linear_tracker(
                f"Operation F: Add progress comment.\n"
                f"Issue ID: {self.linear_issue_id}\n"
                f"Comment: 🔍 **Codebase analyzed.** Planning implementation tasks..."
            )

        # Phase 3 — Plan
        logger.info("[%s] Phase 3: Planning tasks", self._label)
        plan_result = await self._run_planner(self._prompt_plan())
        self.tasks = _parse_task_list(plan_result)

        if self.tasks and self.tasks[0].description.startswith("AMBIGUOUS:"):
            await self._phase_blocked(self.tasks[0].description)
            return

        if self.linear_issue_id:
            task_list = "\n".join(f"{i+1}. {t.title}" for i, t in enumerate(self.tasks))
            await self._run_linear_tracker(
                f"Operation F: Add progress comment.\n"
                f"Issue ID: {self.linear_issue_id}\n"
                f"Comment: 📋 **Plan ready — {len(self.tasks)} tasks:**\n{task_list}"
            )

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
                self.tasks = state.tasks

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

    async def _phase_check_linear(self) -> LinearState:
        logger.info("[%s] Phase 0.5: Checking Linear state", self._label)
        result = await self._run_linear_tracker(
            f"Operation G: Check if a Linear parent issue exists for GitHub issue #{self.event.number}.\n\n"
            f"GitHub issue number: {self.event.number}\n"
            f"GitHub repo full name: {self.event.repo_full_name}\n"
            f"Linear Team ID: {settings.linear_team_id}\n\n"
            f"Return the full reconstruction JSON."
        )
        return _parse_linear_state(result)

    async def _phase_create_sub_issues(self) -> None:
        tasks_needing_id = [t for t in self.tasks if not t.linear_id]
        if not tasks_needing_id or not self.linear_issue_id:
            return

        logger.info("[%s] Phase 4: Creating %d sub-issues in parallel", self._label, len(tasks_needing_id))
        prompts = [
            f"Operation D: Create a sub-issue under the parent Linear issue.\n\n"
            f"Parent Linear issue ID: {self.linear_issue_id}\n"
            f"Task title: {t.title}\n"
            f"Task description: {t.description}\n"
            f"Linear Team ID: {settings.linear_team_id}\n\n"
            f"Return the sub-issue identifier (e.g., MAN-43)."
            for t in tasks_needing_id
        ]
        results = await asyncio.gather(*[self._run_linear_tracker(p) for p in prompts])

        for task, result in zip(tasks_needing_id, results):
            task.linear_id = _extract_linear_id(result)
            if task.linear_id is None:
                logger.warning("[%s] Failed to extract Linear ID for sub-issue '%s' from: %.200s",
                               self._label, task.title, result)
            else:
                logger.info("[%s] Sub-issue '%s' → %s", self._label, task.title, task.linear_id)

    async def _phase_execute_tasks(self) -> bool:
        """Execute all tasks in dependency batches. Returns True if blocked."""
        # 5a — Mark all incomplete tasks In Progress (parallel)
        incomplete = [t for t in self.tasks if t.status != "done" and t.linear_id]
        if incomplete:
            logger.info("[%s] Phase 5a: Marking %d tasks In Progress in parallel", self._label, len(incomplete))
            await asyncio.gather(*[
                self._run_linear_tracker(
                    f"Operation E: Update sub-issue status.\n"
                    f"Sub-issue identifier: {t.linear_id}\n"
                    f"New status: In Progress"
                )
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

        # 5c — Mark all tasks Done (parallel)
        executed = [t for t in self.tasks if t.linear_id]
        if executed:
            logger.info("[%s] Phase 5c: Marking %d tasks Done in parallel", self._label, len(executed))
            await asyncio.gather(*[
                self._run_linear_tracker(
                    f"Operation E: Update sub-issue status.\n"
                    f"Sub-issue identifier: {t.linear_id}\n"
                    f"New status: Done"
                )
                for t in executed
            ])
            for t in executed:
                t.status = "done"

        return False

    async def _phase_test_and_remediate(self, cycle: int = 0) -> bool:
        """Run tests via the tester agent and remediate failures. Returns True if tests pass."""
        if cycle >= settings.max_remediation_cycles:
            if self.linear_issue_id:
                await self._run_linear_tracker(
                    f"Operation F: Add comment.\nIssue ID: {self.linear_issue_id}\n"
                    f"Comment: ⚠️ Tests still failing after {settings.max_remediation_cycles} remediation cycles."
                )
            await self._phase_blocked(
                f"Tests still failing after {settings.max_remediation_cycles} remediation cycles"
            )
            return False

        logger.info("[%s] Phase 5.5: Running tests (cycle %d)", self._label, cycle)
        raw = await self._run_tester(self._prompt_run_tests())
        result = _parse_tester_output(raw)

        if result.passed:
            if self.linear_issue_id:
                await self._run_linear_tracker(
                    f"Operation F: Add comment.\nIssue ID: {self.linear_issue_id}\n"
                    f"Comment: ✅ All tests passing ({result.summary}). Proceeding to review."
                )
            return True

        # Tests failed — create remediation tasks
        logger.info("[%s] Phase 5.5: %d failure(s): %s", self._label, len(result.failures), result.summary)
        if self.linear_issue_id:
            await self._run_linear_tracker(
                f"Operation F: Add comment.\nIssue ID: {self.linear_issue_id}\n"
                f"Comment: 🔧 Test failures (cycle {cycle + 1}/2): {result.summary}"
            )

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
            if self.linear_issue_id:
                await self._run_linear_tracker(
                    f"Operation F: Add comment.\nIssue ID: {self.linear_issue_id}\n"
                    f"Comment: ⚠️ Review issues unresolved after {settings.max_review_cycles} fix attempt(s)."
                )
            await self._phase_blocked(
                f"Review issues unresolved after {settings.max_review_cycles} fix attempt(s)"
            )
            return False

        logger.info("[%s] Phase 5.6: Code review (cycle %d)", self._label, cycle)
        raw = await self._run_reviewer(self._prompt_review())
        result = _parse_reviewer_output(raw)

        if result.approved:
            if self.linear_issue_id:
                await self._run_linear_tracker(
                    f"Operation F: Add comment.\nIssue ID: {self.linear_issue_id}\n"
                    f"Comment: ✅ Code review passed. {result.summary}"
                )
            return True

        # Critical issues found — create fix tasks, apply them, then re-review
        logger.info("[%s] Phase 5.6: %d critical issue(s) found", self._label, len(result.critical_issues))
        if self.linear_issue_id:
            issues_list = "\n".join(f"- {i.get('description', '')}" for i in result.critical_issues)
            await self._run_linear_tracker(
                f"Operation F: Add comment.\nIssue ID: {self.linear_issue_id}\n"
                f"Comment: 🔍 Review cycle {cycle + 1}/{settings.max_review_cycles} — "
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
            self._run_linear_tracker(
                f"Operation E: Update sub-issue status.\n"
                f"Sub-issue identifier: {t.linear_id}\n"
                f"New status: In Progress"
            )
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
            self._run_linear_tracker(
                f"Operation E: Update sub-issue status.\n"
                f"Sub-issue identifier: {t.linear_id}\n"
                f"New status: Done"
            )
            for t in tasks if t.linear_id
        ])
        for t in tasks:
            t.status = "done"

        return False

    async def _phase_submit_pr(self) -> None:
        logger.info("[%s] Phase 6: Submitting PR", self._label)
        files_list = "\n".join(f"- {f}" for f in sorted(set(self.modified_files))) or "(all changed files)"
        result = await self._run_github_submitter(
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
        self.pr_url = _extract_pr_url(result)
        logger.info("[%s] PR URL: %s", self._label, self.pr_url)

    async def _phase_reconcile_subtasks(self) -> None:
        """Safety net: mark any lingering sub-tasks as Done before final update."""
        pending = [t for t in self.tasks if t.linear_id and t.status != "done"]
        if not pending:
            return
        logger.info("[%s] Reconcile: marking %d lingering sub-task(s) Done", self._label, len(pending))
        await asyncio.gather(*[
            self._run_linear_tracker(
                f"Operation E: Update sub-issue status.\n"
                f"Sub-issue identifier: {t.linear_id}\n"
                f"New status: Done"
            )
            for t in pending
        ])
        for t in pending:
            t.status = "done"

    async def _phase_final_linear_update(self) -> None:
        if not self.linear_issue_id:
            return
        await self._phase_reconcile_subtasks()
        logger.info("[%s] Phase 7: Final Linear update", self._label)
        await self._run_linear_tracker(
            f"Operation B: Mark the Linear issue as In Review with the PR URL.\n\n"
            f"Linear issue identifier: {self.linear_issue_id}\n"
            f"PR URL: {self.pr_url or 'N/A'}\n"
            f"Linear project ID: {self.linear_project_id or 'null'}"
        )

    async def _phase_blocked(self, reason: str) -> None:
        logger.warning("[%s] Phase BLOCKED: %s", self._label, reason)
        if not self.linear_issue_id:
            return
        await self._run_linear_tracker(
            f"Operation C: Mark as Needs Clarification.\n\n"
            f"Linear issue identifier: {self.linear_issue_id}\n"
            f"Reason: {reason}"
        )

    # ---------------------------------------------------------------------- #
    # Prompt builders                                                          #
    # ---------------------------------------------------------------------- #

    def _prompt_create_linear_issue(self) -> str:
        return (
            f"Operation A: Create a new Linear issue to track this work.\n\n"
            f"GitHub issue title: {self.event.title}\n"
            f"GitHub issue body: {self.event.body or '(no body)'}\n"
            f"GitHub issue number: {self.event.number}\n"
            f"GitHub issue URL: {self.event.html_url}\n"
            f"GitHub repo full name: {self.event.repo_full_name}\n"
            f"Linear Team ID: {settings.linear_team_id}\n"
            f"Linear Project Name: {self.event.repo_full_name}\n\n"
            f"Return the Linear issue ID (e.g., MAN-42) and the Linear project ID (UUID)."
        )

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
        return (
            f"Break this GitHub issue into implementation tasks.\n\n"
            f"Issue title: {self.event.title}\n"
            f"Issue body: {self.event.body or '(no body)'}\n\n"
            f"Codebase Analysis:\n{self.analysis}\n\n"
            f"Return a raw JSON array of tasks only."
        )

    def _prompt_coder_task(self, task: Task) -> str:
        return (
            f"Implement this specific task for GitHub issue #{self.event.number}.\n\n"
            f"Issue title: {self.event.title}\n"
            f"Issue body: {self.event.body or '(no body)'}\n\n"
            f"Codebase Analysis:\n{self.analysis}\n\n"
            f"Task title: {task.title}\n"
            f"Task description: {task.description}\n"
            f"Files hint: {', '.join(task.files_hint) or 'see analysis'}\n"
            f"Acceptance criteria: {task.acceptance}\n\n"
            f"Local repo path: {self.repo_path}\n\n"
            f"Implement this specific task only. Do NOT run git commands. "
            f"Report all modified files at the end."
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
        return (
            f"Review the code changes made for GitHub issue #{self.event.number}.\n\n"
            f"Issue title: {self.event.title}\n"
            f"Issue body: {self.event.body or '(no body)'}\n\n"
            f"Modified files:\n{files_list}\n\n"
            f"Local repo path: {self.repo_path}\n\n"
            f"Return ONLY the JSON result object — no preamble, no markdown."
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
