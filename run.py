#!/usr/bin/env python3
"""
GitHub Issue Solver — CLI entry point
======================================

Fetches open issues from a GitHub repo via the gh CLI, lets you approve
which ones to solve, then runs the full agent pipeline
(analyze → code → PR → GitHub tracking).

Usage
-----
    # Interactive picker — lists open issues, you choose
    python run.py owner/repo

    # Solve specific issue numbers (no prompt)
    python run.py owner/repo 42
    python run.py owner/repo 42 67 100

    # Solve ALL open issues without prompting
    python run.py owner/repo --all

    # Only consider unassigned issues, with interactive approval
    python run.py owner/repo --unassigned
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()

from config import settings
from models import IssueEvent
from task_runner import get_task_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# gh CLI helpers (works for both public and private repos)                     #
# --------------------------------------------------------------------------- #

def _gh(args: list[str]) -> list | dict:
    """Run a gh CLI command with --json and return parsed output."""
    result = subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)



def fetch_open_issues(owner: str, repo: str, *, unassigned_only: bool = False) -> list[dict]:
    """Return open issues (PRs excluded) via gh CLI."""
    raw: list[dict] = _gh([
        "issue", "list",
        "--repo", f"{owner}/{repo}",
        "--state", "open",
        "--json", "number,title,body,assignees,url",
        "--limit", "100",
    ])

    issues = []
    for item in raw:
        if unassigned_only and item.get("assignees"):
            continue
        # gh issue list excludes PRs by default
        issues.append(item)
    return issues


def fetch_single_issue(owner: str, repo: str, number: int) -> dict | None:
    try:
        return _gh([
            "issue", "view", str(number),
            "--repo", f"{owner}/{repo}",
            "--json", "number,title,body,assignees,url",
        ])
    except Exception as e:
        print(f"  Warning: could not fetch issue #{number}: {e}")
        return None


# --------------------------------------------------------------------------- #
# Interactive approval                                                          #
# --------------------------------------------------------------------------- #

def _print_issue_list(issues: list[dict]) -> None:
    print(f"\n{'─'*70}")
    print(f"  {'#':>5}  TITLE")
    print(f"{'─'*70}")
    for issue in issues:
        num = issue["number"]
        title = issue["title"]
        if len(title) > 57:
            title = title[:57] + "..."
        assignees = ", ".join(a["login"] for a in issue.get("assignees", []))
        tag = f"  [assigned: {assignees}]" if assignees else ""
        print(f"  #{num:<6} {title}{tag}")
    print(f"{'─'*70}\n")


def interactive_select(issues: list[dict]) -> list[dict]:
    """Show issues and let the user pick which ones to solve."""
    if not issues:
        print("No open issues found.")
        return []

    print(f"\nFound {len(issues)} open issue(s):")
    _print_issue_list(issues)
    print("Enter issue numbers to solve (space or comma separated),")
    print("  'all' to solve everything listed, or 'q' to quit.\n")

    issue_map = {str(iss["number"]): iss for iss in issues}

    while True:
        try:
            raw = input("Your selection > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)

        if raw.lower() in ("q", "quit", "exit"):
            print("Exiting.")
            sys.exit(0)

        if raw.lower() == "all":
            print(f"\nSelected all {len(issues)} issue(s).")
            return issues

        tokens = raw.replace(",", " ").split()
        selected: list[dict] = []
        bad: list[str] = []
        for tok in tokens:
            tok = tok.lstrip("#")
            if tok in issue_map:
                selected.append(issue_map[tok])
            else:
                bad.append(tok)

        if bad:
            print(f"Unknown issue number(s): {', '.join(bad)}. Try again.\n")
            continue
        if not selected:
            print("Nothing selected. Try again.\n")
            continue

        print(f"\nSelected {len(selected)} issue(s):")
        for iss in selected:
            print(f"  #{iss['number']}: {iss['title']}")

        try:
            confirm = input("\nProceed? [Y/n] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)

        if confirm in ("", "y", "yes"):
            return selected
        print("Cancelled — pick again.\n")


# --------------------------------------------------------------------------- #
# Main                                                                          #
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Solve GitHub issues automatically via the agent pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("repo", help="Target repository in owner/repo format")
    p.add_argument(
        "issue_numbers",
        nargs="*",
        type=int,
        metavar="ISSUE",
        help="Specific issue number(s) to solve (skips the interactive picker)",
    )
    p.add_argument(
        "--all",
        dest="solve_all",
        action="store_true",
        help="Solve all open issues without prompting",
    )
    p.add_argument(
        "--unassigned",
        action="store_true",
        help="Only consider issues with no assignees",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Attempt to solve issues even if marked as won't implement (Cancelled) in GitHub",
    )
    return p.parse_args()


async def main() -> None:
    args = parse_args()

    if "/" not in args.repo or args.repo.count("/") != 1:
        print(f"Error: repo must be 'owner/repo', got: {args.repo!r}", file=sys.stderr)
        sys.exit(1)

    owner, repo_name = args.repo.split("/", 1)

    # Build repo metadata from CLI args — no API call needed.
    # Cloning uses `gh repo clone` which honours the user's existing gh auth.
    repo_data = {
        "full_name": f"{owner}/{repo_name}",
        "name": repo_name,
        "clone_url": f"https://github.com/{owner}/{repo_name}.git",
        "html_url": f"https://github.com/{owner}/{repo_name}",
    }

    # ------------------------------------------------------------------ #
    # Determine which issues to work on                                    #
    # ------------------------------------------------------------------ #

    if args.issue_numbers:
        print(f"Fetching {len(args.issue_numbers)} specified issue(s)...")
        fetched = await asyncio.gather(*(
            asyncio.to_thread(fetch_single_issue, owner, repo_name, num)
            for num in args.issue_numbers
        ))
        selected_issues = [iss for iss in fetched if iss is not None]

    else:
        print(f"Fetching open issues from {owner}/{repo_name}...")
        all_issues = fetch_open_issues(
            owner, repo_name, unassigned_only=args.unassigned
        )

        if not all_issues:
            label = "unassigned open" if args.unassigned else "open"
            print(f"No {label} issues found in {owner}/{repo_name}.")
            sys.exit(0)

        if args.solve_all:
            selected_issues = all_issues
            print(f"Auto-selecting all {len(selected_issues)} issue(s).")
        else:
            selected_issues = interactive_select(all_issues)

    if not selected_issues:
        print("Nothing to do.")
        sys.exit(0)

    # ------------------------------------------------------------------ #
    # Build IssueEvent objects and hand off to the pipeline               #
    # ------------------------------------------------------------------ #

    events = [IssueEvent.from_api(iss, repo_data, force=args.force) for iss in selected_issues]

    print(f"\nDispatching {len(events)} issue(s) to the agent pipeline...")
    print(f"Max concurrent: {settings.max_concurrent_issues}\n")

    runner = get_task_runner()
    await asyncio.gather(*(runner.dispatch(event) for event in events))

    while runner.active_count > 0:
        await asyncio.sleep(1)

    print("\nAll issues processed.")

    try:
        from token_tracker import print_usage_summary
        print_usage_summary()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
