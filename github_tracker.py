"""GitHub Issues client — drop-in replacement for linear_client.py.

Replaces the Linear GraphQL API with GitHub Issues + Labels + Milestones.
State is tracked via labels (status:in-progress, status:in-review, etc.).
Sub-issues are regular GitHub Issues with a parent reference in the body.

Each public method maps 1-to-1 with the original LinearClient operations:
  A  create_issue       → open a tracking issue in the target repo
  B  mark_in_review     → label + comment with PR URL
  C  mark_cancelled     → label + comment + close
  D  create_sub_issue   → open child issue referencing parent
  E  update_state       → swap status label
  F  add_comment        → post comment
  G  check_state        → reconstruct workflow state from labels/comments
  H  get_comments       → return comment bodies

Backward-compatible aliases (LinearState, LinearTask, get_linear_client) mean
orchestrator.py needs only a one-line import change.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

from config import settings

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"
_STATE_PREFIX = "status:"

# Map human state names → GitHub label names
_STATE_LABELS: dict[str, str] = {
    "In Progress":  "status:in-progress",
    "In Review":    "status:in-review",
    "Done":         "status:done",
    "Cancelled":    "status:cancelled",
    "Canceled":     "status:cancelled",
    "Todo":         "status:todo",
    "Backlog":      "status:todo",
    "Blocked":      "status:blocked",
}

_LABEL_COLORS: dict[str, str] = {
    "status:in-progress": "0075ca",
    "status:in-review":   "e4e669",
    "status:done":        "0e8a16",
    "status:cancelled":   "d93f0b",
    "status:todo":        "cccccc",
    "status:blocked":     "b60205",
    "githubfixer":        "7057ff",
    "sub-issue":          "bfd4f2",
}

_LABEL_TO_STATUS: dict[str, str] = {
    "status:todo":        "todo",
    "status:in-progress": "in_progress",
    "status:done":        "done",
    "status:cancelled":   "done",  # treat as terminal
}

_SUB_ISSUE_COMMENT_RE = re.compile(r"Sub-issue created: #(\d+) — (.+)")


# ---------------------------------------------------------------------------
# Shared data classes (imported by orchestrator)
# ---------------------------------------------------------------------------

@dataclass
class GitHubTrackerTask:
    """Lightweight task representation — mirrors LinearTask."""
    title: str
    description: str
    github_issue_number: int | None = None
    # Keep linear_id for compat — stores GitHub issue number as str
    linear_id: str | None = None
    status: str = "todo"  # "todo", "in_progress", "done"


@dataclass
class GitHubTrackerState:
    """Reconstructed workflow state — mirrors LinearState field-for-field."""
    found: bool
    blocked: bool = False
    in_review: bool = False
    pr_url: str | None = None
    # Keep linear_* names for compat — store GitHub issue/milestone numbers
    linear_issue_id: str | None = None
    linear_project_id: str | None = None
    tasks: list[GitHubTrackerTask] = field(default_factory=list)


# Backward-compatible aliases used by orchestrator.py
LinearTask = GitHubTrackerTask
LinearState = GitHubTrackerState


# ---------------------------------------------------------------------------
# GitHubTrackerClient
# ---------------------------------------------------------------------------

class GitHubTrackerClient:
    """Async GitHub Issues client — drop-in for LinearClient."""

    def __init__(self, token: str, repo_full_name: str) -> None:
        self._repo = repo_full_name  # e.g. "owner/repo"
        self._http = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        self._labels_ensured: bool = False
        self._milestone_cache: dict[str, str] = {}  # name → number str

    # ------------------------------------------------------------------
    # Core helpers
    # ------------------------------------------------------------------

    async def _ensure_labels(self) -> None:
        """Create required labels in the repo if they don't already exist."""
        if self._labels_ensured:
            return
        r = await self._http.get(
            f"{_GH_API}/repos/{self._repo}/labels",
            params={"per_page": 100},
        )
        existing = {lbl["name"] for lbl in r.json()} if r.is_success else set()
        for label, color in _LABEL_COLORS.items():
            if label not in existing:
                await self._http.post(
                    f"{_GH_API}/repos/{self._repo}/labels",
                    json={"name": label, "color": color},
                )
        self._labels_ensured = True

    async def _set_state_label(self, issue_number: int, state_name: str) -> None:
        """Atomically swap the status:* label on an issue."""
        await self._ensure_labels()
        r = await self._http.get(
            f"{_GH_API}/repos/{self._repo}/issues/{issue_number}/labels"
        )
        current = [lbl["name"] for lbl in r.json()] if r.is_success else []
        # Strip all existing status labels
        new_labels = [l for l in current if not l.startswith(_STATE_PREFIX)]
        target = _STATE_LABELS.get(state_name)
        if target:
            new_labels.append(target)
        await self._http.put(
            f"{_GH_API}/repos/{self._repo}/issues/{issue_number}/labels",
            json={"labels": new_labels},
        )

    async def _find_or_create_milestone(self, name: str) -> str | None:
        """Return milestone number (str) for the given name, creating if needed."""
        if name in self._milestone_cache:
            return self._milestone_cache[name]
        r = await self._http.get(
            f"{_GH_API}/repos/{self._repo}/milestones",
            params={"state": "open", "per_page": 100},
        )
        if r.is_success:
            for m in r.json():
                if m["title"] == name:
                    self._milestone_cache[name] = str(m["number"])
                    return self._milestone_cache[name]
        # Create it
        r2 = await self._http.post(
            f"{_GH_API}/repos/{self._repo}/milestones",
            json={"title": name, "description": f"githubFixer tracking for {name}"},
        )
        if r2.is_success:
            num = str(r2.json()["number"])
            self._milestone_cache[name] = num
            return num
        return None

    # ------------------------------------------------------------------
    # Public API — one method per original LinearClient "Operation"
    # ------------------------------------------------------------------

    async def create_issue(
        self,
        title: str,
        description: str,
        project_name: str,
    ) -> tuple[str, str]:
        """Operation A: Open a GitHub issue to track this work.

        Returns (issue_number_str, milestone_number_str) to match
        the LinearClient signature of (identifier, project_id).
        """
        await self._ensure_labels()
        milestone_id = await self._find_or_create_milestone(project_name)
        payload: dict = {
            "title": title,
            "body": description,
            "labels": ["githubfixer", "status:in-progress"],
        }
        if milestone_id:
            payload["milestone"] = int(milestone_id)
        r = await self._http.post(
            f"{_GH_API}/repos/{self._repo}/issues", json=payload
        )
        r.raise_for_status()
        number = str(r.json()["number"])
        logger.info("Created GitHub issue #%s in milestone %s", number, project_name)
        return number, str(milestone_id) if milestone_id else ""

    async def mark_in_review(
        self,
        identifier: str,
        pr_url: str,
        project_id: str | None = None,
    ) -> None:
        """Operation B: Set status:in-review and post the PR URL as a comment."""
        issue_number = int(identifier)
        await self._set_state_label(issue_number, "In Review")
        await self._http.post(
            f"{_GH_API}/repos/{self._repo}/issues/{issue_number}/comments",
            json={"body": f"PR opened: {pr_url}"},
        )

    async def mark_cancelled(self, identifier: str, reason: str) -> None:
        """Operation C: Set status:cancelled, post reason, close the issue."""
        issue_number = int(identifier)
        await self._set_state_label(issue_number, "Cancelled")
        await self._http.post(
            f"{_GH_API}/repos/{self._repo}/issues/{issue_number}/comments",
            json={"body": reason},
        )
        await self._http.patch(
            f"{_GH_API}/repos/{self._repo}/issues/{issue_number}",
            json={"state": "closed", "state_reason": "not_planned"},
        )

    async def create_sub_issue(
        self,
        parent_id: str,
        title: str,
        description: str,
    ) -> str | None:
        """Operation D: Create a child issue referencing the parent.

        Returns the child issue number as a string.
        """
        await self._ensure_labels()
        parent_number = int(parent_id)
        body = f"_Sub-issue of #{parent_number}_\n\n{description}"
        r = await self._http.post(
            f"{_GH_API}/repos/{self._repo}/issues",
            json={
                "title": title,
                "body": body,
                "labels": ["githubfixer", "sub-issue", "status:todo"],
            },
        )
        r.raise_for_status()
        child_number = str(r.json()["number"])
        # Cross-link: post a comment on the parent so check_state can find children
        await self._http.post(
            f"{_GH_API}/repos/{self._repo}/issues/{parent_number}/comments",
            json={"body": f"Sub-issue created: #{child_number} — {title}"},
        )
        logger.info("Created sub-issue #%s under #%s", child_number, parent_number)
        return child_number

    async def update_state(self, identifier: str, state_name: str) -> None:
        """Operation E: Update a sub-issue's status label."""
        issue_number = int(identifier)
        await self._set_state_label(issue_number, state_name)
        if state_name in ("Done", "Cancelled", "Canceled"):
            await self._http.patch(
                f"{_GH_API}/repos/{self._repo}/issues/{issue_number}",
                json={"state": "closed"},
            )

    async def add_comment(self, identifier: str, body: str) -> None:
        """Operation F: Post a progress comment on the issue."""
        await self._http.post(
            f"{_GH_API}/repos/{self._repo}/issues/{identifier}/comments",
            json={"body": body},
        )

    async def check_state(
        self,
        issue_number: int,
        repo_full_name: str,
    ) -> GitHubTrackerState:
        """Operation G: Reconstruct workflow state for a GitHub issue.

        Searches the repo for a githubFixer tracking issue whose title starts
        with "[Auto] #{issue_number}:".  Reconstructs task list from sub-issue
        comments posted by create_sub_issue.
        """
        search_prefix = f"[Auto] #{issue_number}:"
        r = await self._http.get(
            f"{_GH_API}/repos/{repo_full_name}/issues",
            params={"labels": "githubfixer", "state": "all", "per_page": 100},
        )
        if not r.is_success:
            return GitHubTrackerState(found=False)

        matching = [i for i in r.json() if i["title"].startswith(search_prefix)]
        if not matching:
            return GitHubTrackerState(found=False)

        # Most recently created
        issue = sorted(matching, key=lambda i: i["created_at"])[-1]
        labels = {lbl["name"] for lbl in issue.get("labels", [])}
        identifier = str(issue["number"])
        milestone = issue.get("milestone") or {}
        project_id = str(milestone["number"]) if milestone else None

        if "status:cancelled" in labels:
            return GitHubTrackerState(
                found=True,
                blocked=True,
                linear_issue_id=identifier,
                linear_project_id=project_id,
            )

        # Fetch comments to find PR URL and sub-issue links
        cr = await self._http.get(
            f"{_GH_API}/repos/{repo_full_name}/issues/{identifier}/comments"
        )
        comments = cr.json() if cr.is_success else []

        pr_url: str | None = None
        for c in comments:
            body = c.get("body", "")
            if body.startswith("PR opened:"):
                pr_url = body[len("PR opened:"):].strip()
                break

        if "status:in-review" in labels:
            return GitHubTrackerState(
                found=True,
                in_review=True,
                pr_url=pr_url,
                linear_issue_id=identifier,
                linear_project_id=project_id,
            )

        # Reconstruct tasks from sub-issue cross-link comments
        tasks: list[GitHubTrackerTask] = []
        for c in comments:
            body = c.get("body", "")
            m = _SUB_ISSUE_COMMENT_RE.match(body)
            if not m:
                continue
            child_num, child_title = m.group(1), m.group(2)
            cr2 = await self._http.get(
                f"{_GH_API}/repos/{repo_full_name}/issues/{child_num}"
            )
            if not cr2.is_success:
                continue
            child = cr2.json()
            child_label_names = {lbl["name"] for lbl in child.get("labels", [])}
            # Determine status from labels, fall back to open/closed
            status = "todo"
            for lname, lstat in _LABEL_TO_STATUS.items():
                if lname in child_label_names:
                    status = lstat
                    break
            else:
                if child.get("state") == "closed":
                    status = "done"
            tasks.append(GitHubTrackerTask(
                title=child_title,
                description=child.get("body", ""),
                github_issue_number=int(child_num),
                linear_id=child_num,
                status=status,
            ))

        return GitHubTrackerState(
            found=True,
            pr_url=pr_url,
            linear_issue_id=identifier,
            linear_project_id=project_id,
            tasks=tasks,
        )

    async def get_comments(self, identifier: str) -> list[str]:
        """Operation H: Fetch all comment bodies for an issue."""
        r = await self._http.get(
            f"{_GH_API}/repos/{self._repo}/issues/{identifier}/comments"
        )
        if not r.is_success:
            return []
        return [c["body"] for c in r.json() if c.get("body")]

    async def close(self) -> None:
        await self._http.aclose()


# ---------------------------------------------------------------------------
# Module-level singleton + backward-compatible aliases
# ---------------------------------------------------------------------------

_client: GitHubTrackerClient | None = None


def get_github_tracker() -> GitHubTrackerClient:
    global _client
    if _client is None:
        _client = GitHubTrackerClient(settings.github_token, settings.github_repo)
    return _client


# Backward-compatible alias — orchestrator imports this name
def get_linear_client() -> GitHubTrackerClient:
    return get_github_tracker()
