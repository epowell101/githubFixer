# githubFixer

Automatically resolves GitHub issues using a multi-agent Claude pipeline. Point it at a repo, and it analyzes each issue, writes a spec, plans tasks, codes the fix, runs tests, opens a PR, and tracks everything in Linear.

## Prerequisites

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- [GitHub CLI (`gh`)](https://cli.github.com/) installed and authenticated (`gh auth login`)
- A [Linear](https://linear.app) workspace with an API key

## Installation

```bash
git clone https://github.com/mando222/githubFixer.git
cd githubFixer
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```env
# Required
LINEAR_API_KEY=lin_api_...
LINEAR_TEAM_ID=your-team-id

# Optional — override default models
# CODING_AGENT_MODEL=claude-opus-4-6
# SPEC_WRITER_AGENT_MODEL=claude-opus-4-6

# Optional — concurrency
# MAX_CONCURRENT_ISSUES=3
```

Claude authentication is handled through the Claude Code CLI — no API key needed here. Make sure you're logged in (`claude`) before running.

To find your Linear Team ID: go to Linear → Settings → API → scroll to "Team IDs".

## Usage

```bash
source venv/bin/activate

# Interactive picker — lists open issues, you choose which to solve
python run.py owner/repo

# Solve specific issue numbers
python run.py owner/repo 42
python run.py owner/repo 42 67 100

# Solve all open issues without prompting
python run.py owner/repo --all

# Only consider unassigned issues
python run.py owner/repo --unassigned
```

### Example

```bash
python run.py mando222/my-project --all
```

The pipeline will run fully autonomously for each issue:
1. Analyze the codebase
2. Write and review a spec
3. Break the work into tasks and create Linear sub-issues
4. Implement each task, run tests, and self-correct up to 12 cycles
5. Review the implementation against the spec
6. Open a PR on GitHub
7. Mark the Linear ticket "In Review" with the PR link
