from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # GitHub — token needs repo scope; repo is the target for tracking issues
    github_token: str          # GitHub personal access token (repo scope)
    github_repo: str           # e.g. "owner/repo" — where tracking issues are created

    # Models (optional — defaults shown)
    orchestrator_model: str = "claude-sonnet-4-6"
    coding_agent_model: str = "claude-sonnet-4-6"
    tester_agent_model: str = "claude-sonnet-4-6"
    reviewer_agent_model: str = "claude-sonnet-4-6"
    github_agent_model: str = "claude-haiku-4-5-20251001"
    analyzer_agent_model: str = "claude-haiku-4-5-20251001"
    planner_agent_model: str = "claude-haiku-4-5-20251001"
    spec_writer_agent_model: str = "claude-sonnet-4-6"
    spec_reviewer_agent_model: str = "claude-haiku-4-5-20251001"

    # Concurrency
    max_concurrent_planners: int = 5
    max_concurrent_issues: int = 3
    max_concurrent_testers: int = 5

    # Timeouts (seconds)
    issue_timeout_seconds: int = 1800
    planning_timeout_seconds: int = 900

    # GitHub bot login used to filter out bot comments when detecting user replies
    github_bot_login: str = "github-actions[bot]"

    # Max cycles before blocking
    max_remediation_cycles: int = 3
    max_review_cycles: int = 2
    max_fix_tasks_per_review_cycle: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
