# githubFixer

Automatically resolves GitHub issues using a multi-agent Claude pipeline. Point it at a repo, and it analyzes each issue, writes a spec, plans tasks, codes the fix, runs tests, opens a PR, and tracks everything in GitHub.

## Prerequisites

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- [GitHub CLI (`gh`)](https://cli.github.com/) installed and authenticated (`gh auth login`)
- A GitHub personal access token with Issues, Contents, and Pull Requests read/write permissions

## Installation

```bash
git clone https://github.com/epowell101/githubFixer.git
cd githubFixer
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```env
# Required
GITHUB_TOKEN=ghp_...
GITHUB_REPO=owner/repo
ANTHROPIC_API_KEY=sk-ant-...

# Optional — override default models
# CODING_AGENT_MODEL=claude-opus-4-6
# SPEC_WRITER_AGENT_MODEL=claude-opus-4-6

# Optional — concurrency
# MAX_CONCURRENT_ISSUES=3
```

Create your GitHub token at: GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens. It needs read/write permissions for Issues, Contents, and Pull Requests on the target repo.

## Usage

```bash
source .venv/bin/activate

# Interactive picker — lists open issues, you choose which to solve
python run.py owner/repo

# Solve specific issue numbers
python run.py owner/repo 42
python run.py owner/repo 42 67 100

# Solve all open issues without prompting
python run.py owner/repo --all

# Only consider unassigned issues
python run.py owner/repo --unassigned

# Force-solve an issue even if it's closed or marked won't-implement
python run.py owner/repo 42 --force
python run.py owner/repo --all --force
```

### `--force`

By default the pipeline skips issues whose GitHub tracking state is `Cancelled` or `Won't Implement`. Pass `--force` to override that check — the issue is reactivated to *In Progress* and the full pipeline runs regardless of its current state. Useful for revisiting deliberately-closed tickets or retrying a previously-rejected fix.

### Example

```bash
python run.py epowell101/my-project --all
python run.py epowell101/my-project 42 67 100        # three issues in parallel
python run.py epowell101/my-project 55 --force       # reopen and retry a closed issue
```

### Parallel execution

Whether you pass `--all` or explicit issue numbers, all selected issues are dispatched concurrently up to `MAX_CONCURRENT_ISSUES` (default: 3). Each issue gets its own isolated git worktree and independent agent pipeline — they do not share state or block each other.

The pipeline will run fully autonomously for each issue:
1. Analyze the codebase
2. Write and review a spec
3. Break the work into tasks and create GitHub sub-issues
4. Implement each task, run tests, and self-correct up to 12 cycles
5. Review the implementation against the spec
6. Open a PR on GitHub
7. Mark the GitHub tracking issue "In Review" with the PR link
