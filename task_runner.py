from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from config import settings

if TYPE_CHECKING:
    from models import IssueEvent

logger = logging.getLogger(__name__)


class TaskRunner:
    def __init__(self, max_concurrent: int | None = None) -> None:
        limit = max_concurrent or settings.max_concurrent_issues
        self._semaphore = asyncio.Semaphore(limit)
        self._active: dict[str, asyncio.Task] = {}

    async def dispatch(self, event: "IssueEvent") -> None:
        key = f"{event.repo_full_name}#{event.number}"

        # Dedup: don't start a second task for an already-running issue
        existing = self._active.get(key)
        if existing and not existing.done():
            logger.info("Issue %s is already being processed — skipping duplicate", key)
            return

        logger.info("Dispatching issue %s", key)
        task = asyncio.create_task(self._run(event, key))
        self._active[key] = task

    async def _run(self, event: "IssueEvent", key: str) -> None:
        from agents.orchestrator import run_issue_planning, run_issue_execution

        try:
            # Planning runs immediately — no semaphore — so all issues appear in
            # Linear while execution is throttled.
            try:
                await asyncio.wait_for(
                    run_issue_planning(event),
                    timeout=settings.planning_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Issue %s planning timed out after %ds",
                    key, settings.planning_timeout_seconds,
                )
            except Exception:
                logger.exception("Unhandled error planning issue %s", key)

            # Execution is throttled to max_concurrent_issues in parallel
            async with self._semaphore:
                try:
                    await asyncio.wait_for(
                        run_issue_execution(event),
                        timeout=settings.issue_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "Issue %s execution timed out after %ds",
                        key, settings.issue_timeout_seconds,
                    )
                except Exception:
                    logger.exception("Unhandled error executing issue %s", key)
        finally:
            self._active.pop(key, None)
            logger.info("Finished processing issue %s", key)

    @property
    def active_count(self) -> int:
        return sum(1 for t in self._active.values() if not t.done())


# Module-level singleton
_runner: TaskRunner | None = None


def get_task_runner() -> TaskRunner:
    global _runner
    if _runner is None:
        _runner = TaskRunner()
    return _runner
