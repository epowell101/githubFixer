from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Linear — find these in Linear > Settings > API
    linear_api_key: str  # Linear > Settings > API > Personal API Keys
    linear_team_id: str
    linear_in_progress_state_id: str = ""
    linear_in_review_state_id: str = ""
    linear_done_state_id: str = ""
    linear_needs_clarification_state_id: str = ""

    # Models (optional — defaults shown)
    orchestrator_model: str = "claude-sonnet-4-6"
    coding_agent_model: str = "claude-sonnet-4-6"
    tester_agent_model: str = "claude-sonnet-4-6"
    reviewer_agent_model: str = "claude-sonnet-4-6"
    github_agent_model: str = "claude-haiku-4-5-20251001"
    analyzer_agent_model: str = "claude-haiku-4-5-20251001"
    planner_agent_model: str = "claude-haiku-4-5-20251001"
    spec_writer_agent_model: str = "claude-sonnet-4-6"  # override to claude-opus-4-6 via .env
    spec_reviewer_agent_model: str = "claude-haiku-4-5-20251001"  # pure comparison, no tools

    # Concurrency — direct Linear API supports concurrent connections
    max_concurrent_planners: int = 5   # planning has no semaphore by default; cap at 5 concurrent
    max_concurrent_issues: int = 3
    # Testers can run more broadly in parallel than coders (I/O-bound, not CPU-bound)
    max_concurrent_testers: int = 5

    # How long (seconds) to allow a single issue workflow before timing out
    issue_timeout_seconds: int = 1800  # 30 minutes

    # How long (seconds) to allow planning phases (0.5-4) before timing out
    planning_timeout_seconds: int = 900  # 15 minutes (increased for spec writer + reviewer phases)

    # GitHub bot login used to filter out bot comments when detecting user replies
    github_bot_login: str = "github-actions[bot]"

    # Max code→test→fix cycles before blocking
    max_remediation_cycles: int = 3
    # Max review-fix-re-review cycles before blocking
    max_review_cycles: int = 2



@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
