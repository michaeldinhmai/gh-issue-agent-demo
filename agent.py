"""A minimal agentic loop: Claude answers questions about a GitHub backlog by
choosing which GitHub Issues API calls to make.

The program does not decide which issues are relevant. It exposes two tools,
describes them, and lets the model plan the calls. Run with:

    python agent.py "what's blocked right now and why"
"""

import json
import os
import subprocess
import sys

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()


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
        "strict": True,
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


def ask(question, max_turns=10):
    """Run the agentic loop until the model stops requesting tools."""
    client = anthropic.Anthropic()
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
