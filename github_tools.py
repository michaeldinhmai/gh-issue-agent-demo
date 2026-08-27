"""GitHub Issues tools, with no knowledge of any model or host.

This module is the reason the same three functions can serve two completely
different consumers without modification:

  * agent.py       - a hand-written tool-use loop that this repo owns
  * mcp_server.py  - an MCP server, where Claude Desktop / Claude Code /
                     ChatGPT owns the loop instead

Nothing below imports anthropic, and nothing below knows what a "turn" is.
That separation is the whole architecture.
"""

import datetime
import os
import pathlib
import subprocess

import requests
from dotenv import load_dotenv

# Anchored to this file, not the process working directory. An MCP host
# launches mcp_server.py from wherever it likes, and a bare load_dotenv()
# would silently find nothing.
load_dotenv(pathlib.Path(__file__).parent / ".env")


def _github_token():
    """Prefer GITHUB_TOKEN from .env; otherwise borrow the gh CLI's token.

    The fallback means a developer with `gh` already authenticated never has
    to copy a PAT into a file on disk.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and not token.startswith("ghp_..."):
        return token
    try:
        return subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit(
            "No GitHub credential. Set GITHUB_TOKEN in .env or run `gh auth login`."
        )


GITHUB_API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPO", "").strip() or "michaeldinhmai/gh-issue-agent-demo"
GITHUB_TOKEN = _github_token()


# --------------------------------------------------------------------------
# Tool implementations - plain Python functions hitting the GitHub REST API.
# --------------------------------------------------------------------------

def _gh(path, params=None):
    """GET a GitHub REST endpoint and return parsed JSON."""
    response = requests.get(
        f"{GITHUB_API}{path}",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _issue_rows(state="open", labels=None, assignee=None):
    """The projection itself, as a plain list. Internal - callers get an
    envelope, so that a zero-length result still says what it looked for."""
    params = {"state": state, "per_page": 100}
    if labels:
        params["labels"] = labels
    if assignee:
        params["assignee"] = assignee

    issues = _gh(f"/repos/{REPO}/issues", params)

    return [
        {
            "number": issue["number"],
            "title": issue["title"],
            "state": issue["state"],
            "labels": [label["name"] for label in issue["labels"]],
            "assignee": issue["assignee"]["login"] if issue["assignee"] else None,
            "comments": issue["comments"],
            "created_at": issue["created_at"],
            "updated_at": issue["updated_at"],
            "body": (issue.get("body") or "")[:600],
        }
        # The issues endpoint also returns pull requests; they have a
        # "pull_request" key. Drop them so the model isn't reasoning over PRs
        # it was never asked about.
        for issue in issues
        if "pull_request" not in issue
    ]


# Every tool returns an envelope rather than a bare list. A bare [] is
# ambiguous to a model: it cannot tell "nothing matched" from "the call
# failed and returned nothing". Restating the filters alongside a count makes
# absence an explicit, quotable fact - and stops the model from silently
# retrying a query that already answered correctly.

def list_issues(state="open", labels=None, assignee=None):
    """Return a compact projection of repo issues.

    GitHub returns ~80 fields per issue. Everything the model does not need is
    dropped here: the tool result is context the model pays for on every
    subsequent turn of the loop.
    """
    rows = _issue_rows(state=state, labels=labels, assignee=assignee)
    return {
        "filters": {"state": state, "labels": labels, "assignee": assignee},
        "count": len(rows),
        "issues": rows,
    }


def _now():
    """Current UTC time, overridable via AGENT_NOW for tests and demos.

    An injected clock is the only way to test time-dependent logic without
    sleeping. It doubles as a demo affordance: every seed issue in this repo
    was created in the same minute, so `AGENT_NOW=2026-11-01` is what makes
    find_stale_issues return anything interesting.
    """
    override = os.environ.get("AGENT_NOW", "").strip()
    if override:
        stamp = datetime.datetime.fromisoformat(override)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return stamp
    return datetime.datetime.now(datetime.timezone.utc)


def _days_since(timestamp, now=None):
    """Whole days between a GitHub ISO-8601 timestamp and now. Pure function."""
    then = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return ((now or _now()) - then).days


def find_stale_issues(days=30, state="open", labels=None):
    """Issues untouched for at least `days` days, newest-stale first.

    Deliberately built on top of the same projection rather than a new
    endpoint: one API call, and the staleness math is ours, not GitHub's.
    There is no label for "rotting", so this is a question the model cannot
    answer by filtering - only by computing.
    """
    now = _now()
    stale = []
    for issue in _issue_rows(state=state, labels=labels):
        age = _days_since(issue["updated_at"], now)
        if age >= int(days):
            stale.append({**issue, "stale_days": age})
    stale.sort(key=lambda issue: issue["stale_days"], reverse=True)

    return {
        # State the clock. Otherwise the model has no way to know whether
        # "0 days stale" means the repo is active or the clock is wrong.
        "as_of": now.isoformat(),
        "threshold_days": int(days),
        "filters": {"state": state, "labels": labels},
        "count": len(stale),
        "issues": stale,
    }


def get_issue_comments(issue_number):
    """Return the comment thread on one issue, author and body only."""
    comments = _gh(f"/repos/{REPO}/issues/{int(issue_number)}/comments",
                   {"per_page": 100})
    rows = [
        {
            "author": comment["user"]["login"],
            "created_at": comment["created_at"],
            "body": comment["body"],
        }
        for comment in comments
    ]
    return {
        "issue_number": int(issue_number),
        "comment_count": len(rows),
        "comments": rows,
    }


# --------------------------------------------------------------------------
# Tool schemas - what the model sees. The description and the parameter docs
# are the entire basis for the model's routing decision, so they carry real
# weight: this is prompt engineering, not documentation.
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_issues",
        "description": (
            "List issues in the repository, optionally filtered by state, "
            "label, or assignee. Returns a count and the filters that were "
            "applied, then the issues themselves: number, title, state, "
            "labels, assignee, comment count, timestamps, and a truncated "
            "body. A count of 0 means nothing matched - not that the call "
            "failed, so do not retry it unchanged. Use this first to find "
            "candidate issues. The comment count tells you whether an issue "
            "has discussion worth fetching."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Issue state filter. Defaults to open.",
                },
                "labels": {
                    "type": "string",
                    "description": (
                        "Comma-separated label names. An issue must carry ALL "
                        "of them to match, so prefer one label per call and "
                        "make several calls if you need a union."
                    ),
                },
                "assignee": {
                    "type": "string",
                    "description": (
                        "GitHub login to filter by. Pass 'none' for issues "
                        "with no assignee, '*' for any assignee."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_stale_issues",
        "description": (
            "Find issues that have not been updated in at least N days, "
            "sorted most-stale first. Each result carries a stale_days field, "
            "and the response states the as_of date the ages were computed "
            "against - trust that date over any assumption about today. "
            "There is no label for staleness, so this is the only way to "
            "answer questions about neglect, rot, or things that have gone "
            "quiet. Combine it with the labels argument to ask sharper "
            "questions - for example stale blocked issues, where the blocker "
            "itself may no longer be current."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Minimum days since last update. Defaults to 30.",
                },
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Issue state filter. Defaults to open.",
                },
                "labels": {
                    "type": "string",
                    "description": "Optional comma-separated label filter.",
                },
            },
            "required": ["days"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_issue_comments",
        "description": (
            "Fetch the full comment thread on a single issue by number. Returns "
            "a comment_count alongside the comments, so an empty thread is "
            "stated rather than implied. Use "
            "this when the issue title and truncated body do not explain the "
            "situation - for example to find out WHY something is blocked, or "
            "what the latest status is. Costs one API call per issue, so call "
            "it only on issues that matter to the question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_number": {
                    "type": "integer",
                    "description": "The issue number, e.g. 7.",
                },
            },
            "required": ["issue_number"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


TOOL_FUNCTIONS = {
    "list_issues": list_issues,
    "find_stale_issues": find_stale_issues,
    "get_issue_comments": get_issue_comments,
}
