"""linear_client.py — compatibility shim.

This module has been replaced by github_tracker.py, which implements the
same interface using GitHub Issues + Labels instead of the Linear GraphQL API.

All names are re-exported so any code that still imports from linear_client
continues to work without modification.
"""
from github_tracker import (  # noqa: F401
    GitHubTrackerClient,
    GitHubTrackerState,
    GitHubTrackerTask,
    LinearClient,
    LinearState,
    LinearTask,
    get_github_tracker,
    get_linear_client,
)

# Convenience alias kept for backward compatibility
LinearClient = GitHubTrackerClient
