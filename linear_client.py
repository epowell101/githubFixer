"""Direct Python client for the Linear GraphQL API.

Replaces the linear-tracker LLM agent, which was spawned ~28 times per issue
workflow to make zero-reasoning structured API calls.  Each public method maps
to one of the eight "Operations" previously described in the linear_tracker
system prompt.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from config import settings

logger = logging.getLogger(__name__)

_GQL_URL = "https://api.linear.app/graphql"


# --------------------------------------------------------------------------- #
# Shared data classes (imported by orchestrator)                               #
# --------------------------------------------------------------------------- #

@dataclass
class LinearTask:
    """Lightweight task representation used in LinearState reconstruction."""
    title: str
    description: str
    linear_id: str | None = None
    status: str = "todo"  # "todo", "in_progress", "done"


@dataclass
class LinearState:
    """Reconstructed workflow state recovered from Linear."""
    found: bool
    blocked: bool = False
    in_review: bool = False
    pr_url: str | None = None
    linear_issue_id: str | None = None
    linear_project_id: str | None = None
    tasks: list[LinearTask] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# GraphQL query/mutation strings                                               #
# --------------------------------------------------------------------------- #

_LIST_PROJECTS = """
query ListProjects($query: String!) {
    projects(filter: {name: {containsIgnoreCase: $query}}) {
        nodes { id name }
    }
}
"""

_CREATE_PROJECT = """
mutation CreateProject($name: String!, $teamId: String!, $description: String!) {
    projectCreate(input: {
        name: $name
        description: $description
        teamIds: [$teamId]
    }) {
        success
        project { id }
    }
}
"""

_CREATE_ISSUE = """
mutation CreateIssue(
    $title: String!, $description: String!,
    $teamId: String!, $stateId: String!,
    $projectId: String, $parentId: String
) {
    issueCreate(input: {
        title: $title
        description: $description
        teamId: $teamId
        stateId: $stateId
        projectId: $projectId
        parentId: $parentId
    }) {
        success
        issue { id identifier }
    }
}
"""

_UPDATE_ISSUE = """
mutation UpdateIssue($id: String!, $stateId: String!) {
    issueUpdate(id: $id, input: { stateId: $stateId }) {
        success
        issue { id }
    }
}
"""

_CREATE_COMMENT = """
mutation CreateComment($issueId: String!, $body: String!) {
    commentCreate(input: { issueId: $issueId, body: $body }) {
        success
        comment { id }
    }
}
"""

_SEARCH_ISSUES = """
query SearchIssues($teamId: String!, $query: String!) {
    issues(
        filter: {
            team: { id: { eq: $teamId } }
            title: { contains: $query }
        }
        orderBy: createdAt
    ) {
        nodes {
            id identifier title createdAt
            state { name type }
            project { id }
        }
    }
}
"""

_GET_ISSUE_FULL = """
query GetIssueFull($id: String!) {
    issue(id: $id) {
        id identifier title
        state { name }
        project { id }
        children {
            nodes {
                identifier title description
                state { name }
            }
        }
        comments {
            nodes { body }
        }
    }
}
"""

_TEAM_STATES = """
query TeamStates($teamId: String!) {
    workflowStates(filter: { team: { id: { eq: $teamId } } }) {
        nodes { id name type }
    }
}
"""


# --------------------------------------------------------------------------- #
# LinearClient                                                                 #
# --------------------------------------------------------------------------- #

class LinearClient:
    """Async direct client for the Linear GraphQL API."""

    def __init__(self, api_key: str, team_id: str) -> None:
        self._team_id = team_id
        self._state_cache: dict[str, str] = {}    # state name → UUID
        self._project_cache: dict[str, str] = {}  # project name → UUID
        self._http = httpx.AsyncClient(
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    # ------------------------------------------------------------------ #
    # Core helpers                                                         #
    # ------------------------------------------------------------------ #

    async def _query(self, query: str, variables: dict | None = None) -> dict:
        resp = await self._http.post(
            _GQL_URL,
            json={"query": query, "variables": variables or {}},
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"Linear GraphQL error: {data['errors']}")
        return data["data"]

    async def _resolve_state_id(self, state_name: str) -> str:
        """Return the UUID for a workflow state name.

        Checks pre-configured IDs from settings first, then falls back to
        a lazy fetch of all team workflow states.
        """
        configured: dict[str, str] = {
            "In Progress": settings.linear_in_progress_state_id,
            "In Review": settings.linear_in_review_state_id,
            "Done": settings.linear_done_state_id,
            "Cancelled": settings.linear_needs_clarification_state_id,
            "Canceled": settings.linear_needs_clarification_state_id,
        }
        cfg_id = configured.get(state_name, "")
        if cfg_id:
            return cfg_id

        if not self._state_cache:
            data = await self._query(_TEAM_STATES, {"teamId": self._team_id})
            for node in data["workflowStates"]["nodes"]:
                self._state_cache[node["name"]] = node["id"]

        if state_name in self._state_cache:
            return self._state_cache[state_name]

        # Fallback: match case-insensitively
        lower = state_name.lower()
        for name, uid in self._state_cache.items():
            if name.lower() == lower:
                return uid

        raise ValueError(
            f"Unknown Linear workflow state {state_name!r} for team {self._team_id!r}"
        )

    async def _find_or_create_project(self, name: str) -> str:
        """Return the UUID for the named Linear project, creating it if needed."""
        if name in self._project_cache:
            return self._project_cache[name]

        data = await self._query(_LIST_PROJECTS, {"query": name})
        for node in data["projects"]["nodes"]:
            if node["name"] == name:
                self._project_cache[name] = node["id"]
                return node["id"]

        data = await self._query(_CREATE_PROJECT, {
            "name": name,
            "teamId": self._team_id,
            "description": f"Automated issue tracking for GitHub repo {name}",
        })
        project_id = data["projectCreate"]["project"]["id"]
        self._project_cache[name] = project_id
        return project_id

    # ------------------------------------------------------------------ #
    # Public API — one method per "Operation"                              #
    # ------------------------------------------------------------------ #

    async def create_issue(
        self,
        title: str,
        description: str,
        project_name: str,
    ) -> tuple[str, str]:
        """Operation A: Create a new Linear issue to track this work.

        Returns (issue_identifier, project_id).
        """
        state_id = await self._resolve_state_id("In Progress")
        project_id = await self._find_or_create_project(project_name)

        data = await self._query(_CREATE_ISSUE, {
            "title": title,
            "description": description,
            "teamId": self._team_id,
            "stateId": state_id,
            "projectId": project_id,
        })
        identifier = data["issueCreate"]["issue"]["identifier"]
        logger.info("Created Linear issue %s in project %s", identifier, project_name)
        return identifier, project_id

    async def mark_in_review(
        self,
        identifier: str,
        pr_url: str,
        project_id: str | None = None,
    ) -> None:
        """Operation B: Mark issue as In Review and post the PR URL as a comment."""
        state_id = await self._resolve_state_id("In Review")
        try:
            await self._query(_UPDATE_ISSUE, {"id": identifier, "stateId": state_id})
        except Exception as exc:
            # Issue may be archived — reactivate first then retry
            if "not found" in str(exc).lower() or "entity" in str(exc).lower():
                logger.warning("Linear issue %s not found, trying to reactivate: %s", identifier, exc)
                in_prog_id = await self._resolve_state_id("In Progress")
                await self._query(_UPDATE_ISSUE, {"id": identifier, "stateId": in_prog_id})
                await self._query(_UPDATE_ISSUE, {"id": identifier, "stateId": state_id})
            else:
                raise
        await self._query(_CREATE_COMMENT, {"issueId": identifier, "body": f"PR opened: {pr_url}"})

    async def mark_cancelled(self, identifier: str, reason: str) -> None:
        """Operation C: Mark issue as Cancelled/Needs Clarification."""
        state_id = await self._resolve_state_id("Cancelled")
        await self._query(_UPDATE_ISSUE, {"id": identifier, "stateId": state_id})
        await self._query(_CREATE_COMMENT, {"issueId": identifier, "body": reason})

    async def create_sub_issue(
        self,
        parent_id: str,
        title: str,
        description: str,
    ) -> str | None:
        """Operation D: Create a sub-issue under a parent. Returns identifier."""
        state_id = await self._resolve_state_id("Todo")
        data = await self._query(_CREATE_ISSUE, {
            "title": title,
            "description": description,
            "teamId": self._team_id,
            "stateId": state_id,
            "parentId": parent_id,
        })
        identifier = data["issueCreate"]["issue"]["identifier"]
        logger.info("Created sub-issue %s under %s", identifier, parent_id)
        return identifier

    async def update_state(self, identifier: str, state_name: str) -> None:
        """Operation E: Update a sub-issue's status."""
        state_id = await self._resolve_state_id(state_name)
        await self._query(_UPDATE_ISSUE, {"id": identifier, "stateId": state_id})

    async def add_comment(self, identifier: str, body: str) -> None:
        """Operation F: Add a progress comment to a Linear issue."""
        await self._query(_CREATE_COMMENT, {"issueId": identifier, "body": body})

    async def check_state(
        self,
        issue_number: int,
        repo_full_name: str,
    ) -> LinearState:
        """Operation G: Query existing Linear state for a GitHub issue.

        Returns a LinearState with full task reconstruction when found.
        """
        search_query = f"[Auto] #{issue_number}:"
        data = await self._query(_SEARCH_ISSUES, {
            "teamId": self._team_id,
            "query": search_query,
        })
        nodes = data["issues"]["nodes"]

        # Filter to issues whose title starts with the search prefix
        matching = [n for n in nodes if n["title"].startswith(search_query)]
        if not matching:
            return LinearState(found=False)

        # Pick most recently created (last in createdAt-ordered list)
        issue = matching[-1]
        state_name = issue.get("state", {}).get("name", "")

        if state_name == "Archived":
            return LinearState(found=False)

        identifier = issue["identifier"]
        project_id: str | None = (issue.get("project") or {}).get("id")

        if state_name in ("Cancelled", "Canceled"):
            return LinearState(
                found=True,
                blocked=True,
                linear_issue_id=identifier,
                linear_project_id=project_id,
            )

        # Fetch full issue data: children + comments
        full_data = await self._query(_GET_ISSUE_FULL, {"id": identifier})
        full = full_data["issue"]

        # Check for PR URL in comments
        pr_url: str | None = None
        for comment in (full.get("comments") or {}).get("nodes", []):
            body = comment.get("body", "")
            if body.startswith("PR opened:"):
                pr_url = body[len("PR opened:"):].strip()
                break

        if state_name == "In Review":
            return LinearState(
                found=True,
                in_review=True,
                pr_url=pr_url,
                linear_issue_id=identifier,
                linear_project_id=project_id,
            )

        # Reconstruct tasks from sub-issues
        _status_map = {
            "Todo": "todo", "Backlog": "todo",
            "In Progress": "in_progress", "Started": "in_progress",
            "Done": "done", "Completed": "done",
        }
        tasks: list[LinearTask] = []
        for child in (full.get("children") or {}).get("nodes", []):
            child_state = (child.get("state") or {}).get("name", "")
            tasks.append(LinearTask(
                title=child.get("title", ""),
                description=child.get("description", ""),
                linear_id=child.get("identifier"),
                status=_status_map.get(child_state, "todo"),
            ))

        # Recover project_id from issue if not already set
        if not project_id:
            project_id = (full.get("project") or {}).get("id")

        return LinearState(
            found=True,
            pr_url=pr_url,
            linear_issue_id=identifier,
            linear_project_id=project_id,
            tasks=tasks,
        )

    async def get_comments(self, identifier: str) -> list[str]:
        """Operation H: Fetch all comment bodies for a Linear issue."""
        data = await self._query(_GET_ISSUE_FULL, {"id": identifier})
        nodes = (data["issue"].get("comments") or {}).get("nodes", [])
        return [n["body"] for n in nodes if n.get("body")]


# --------------------------------------------------------------------------- #
# Module-level singleton                                                       #
# --------------------------------------------------------------------------- #

_client: LinearClient | None = None


def get_linear_client() -> LinearClient:
    global _client
    if _client is None:
        _client = LinearClient(settings.linear_api_key, settings.linear_team_id)
    return _client
