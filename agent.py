"""A minimal agentic loop: Claude answers questions about a GitHub backlog by
choosing which GitHub Issues API calls to make.

The program does not decide which issues are relevant. It exposes two tools,
describes them, and lets the model plan the calls. Run with:

    python agent.py "what's blocked right now and why"
"""

import datetime
import json
import os
import subprocess
import sys

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

# Claude writes em-dashes and arrows; the Windows console defaults to cp1252
# and would mangle them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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


MODEL = "claude-opus-5"
GITHUB_API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPO", "").strip() or "michaeldinhmai/gh-issue-agent-demo"
GITHUB_TOKEN = _github_token()

VERBOSE = os.environ.get("AGENT_VERBOSE", "1") != "0"


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


def list_issues(state="open", labels=None, assignee=None):
    """Return a compact projection of repo issues.

    GitHub returns ~80 fields per issue. Everything the model does not need is
    dropped here: the tool result is context the model pays for on every
    subsequent turn of the loop.
    """
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

    Deliberately built on top of list_issues rather than a new endpoint: one
    API call, and the staleness math is ours, not GitHub's. There is no label
    for "rotting", so this is a question the model cannot answer by
    filtering - only by computing.
    """
    now = _now()
    stale = []
    for issue in list_issues(state=state, labels=labels):
        age = _days_since(issue["updated_at"], now)
        if age >= int(days):
            stale.append({**issue, "stale_days": age})
    return sorted(stale, key=lambda issue: issue["stale_days"], reverse=True)


def get_issue_comments(issue_number):
    """Return the comment thread on one issue, author and body only."""
    comments = _gh(f"/repos/{REPO}/issues/{int(issue_number)}/comments",
                   {"per_page": 100})
    return [
        {
            "author": comment["user"]["login"],
            "created_at": comment["created_at"],
            "body": comment["body"],
        }
        for comment in comments
    ]


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
            "label, or assignee. Returns issue number, title, state, labels, "
            "assignee, comment count, timestamps, and a truncated body. "
            "Use this first to find candidate issues. The comment count tells "
            "you whether an issue has discussion worth fetching."
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
            "sorted most-stale first. Each result carries a stale_days field. "
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
            "Fetch the full comment thread on a single issue by number. Use "
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

SYSTEM_PROMPT = (
    f"You are a project analyst for the GitHub repository {REPO}. Answer "
    "questions about the backlog using only the tools provided; never invent "
    "issue numbers, labels, or assignees. Plan your calls: start broad with "
    "list_issues, then pull comment threads only where they change the "
    "answer. Cite issues as #<number> so the reader can look them up. Be "
    "concise and lead with the answer, not with a description of your process."
)


def run_tool(name, tool_input):
    """Dispatch one tool_use block to the matching Python function."""
    try:
        result = TOOL_FUNCTIONS[name](**tool_input)
        return json.dumps(result), False
    except Exception as exc:
        # Returned to the model as a tool_result with is_error=True so it can
        # recover (retry with different arguments) instead of the loop dying.
        return f"{type(exc).__name__}: {exc}", True


def ask(question, max_turns=10, client=None, trace=None):
    """Run the agentic loop until the model stops requesting tools.

    `client` is injectable so the loop's message-protocol behaviour can be
    tested against a fake without spending tokens. Pass a list as `trace` to
    record every tool call the model made - that record, not the prose, is
    what the evals assert on.
    """
    client = client or anthropic.Anthropic()
    messages = [{"role": "user", "content": question}]

    for turn in range(max_turns):
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # The assistant turn must go back verbatim - tool_use blocks and any
        # thinking blocks included. The API is stateless; this list is the
        # only memory the loop has.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            text = "\n".join(b.text for b in response.content if b.type == "text")
            if VERBOSE:
                print(f"\n[turn {turn + 1}] stop_reason={response.stop_reason} "
                      f"| in={response.usage.input_tokens} "
                      f"out={response.usage.output_tokens}")
            return text

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if VERBOSE:
                print(f"[turn {turn + 1}] -> {block.name}({json.dumps(block.input)})")
            content, is_error = run_tool(block.name, block.input)
            if trace is not None:
                trace.append({"turn": turn + 1, "tool": block.name,
                              "input": block.input, "is_error": is_error})
            if VERBOSE:
                preview = content if len(content) < 160 else content[:160] + "..."
                print(f"[turn {turn + 1}] <- {'ERROR ' if is_error else ''}{preview}")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
                "is_error": is_error,
            })

        # All results from one assistant turn go back in a SINGLE user
        # message. Splitting them across messages teaches the model to stop
        # issuing parallel tool calls.
        messages.append({"role": "user", "content": tool_results})

    return "Stopped: hit the max_turns limit without a final answer."


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    print(ask(" ".join(sys.argv[1:])))


if __name__ == "__main__":
    main()
