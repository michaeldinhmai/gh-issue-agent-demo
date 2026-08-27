"""The same three tools, exposed over MCP so a host application owns the loop.

Run `agent.py` and this repo supplies the agentic loop. Register this file
with Claude Desktop, Claude Code, or ChatGPT and that application supplies
it instead - the `while stop_reason == "tool_use"` machine still runs, it
just runs inside someone else's process.

Note what is absent here: no anthropic import, no API key, no message list,
no max_turns. Note what is identical: the tool functions, and the
descriptions. Those descriptions are still the entire basis for the routing
decision, so the prompt engineering carries over unchanged.

    python mcp_server.py          # stdio transport, what MCP hosts speak
"""

from mcp.server.fastmcp import FastMCP

import github_tools

mcp = FastMCP("gh-issue-agent")


@mcp.tool()
def list_issues(state: str = "open", labels: str = "", assignee: str = "") -> list:
    """List issues in the repository, optionally filtered by state, label, or
    assignee. Returns issue number, title, state, labels, assignee, comment
    count, timestamps, and a truncated body. Use this first to find candidate
    issues. The comment count tells you whether an issue has discussion worth
    fetching.

    Args:
        state: One of open, closed, all. Defaults to open.
        labels: Comma-separated label names. An issue must carry ALL of them
            to match, so prefer one label per call and make several calls if
            you need a union.
        assignee: GitHub login to filter by. Pass 'none' for issues with no
            assignee, '*' for any assignee.
    """
    return github_tools.list_issues(
        state=state, labels=labels or None, assignee=assignee or None
    )


@mcp.tool()
def find_stale_issues(days: int = 30, state: str = "open", labels: str = "") -> list:
    """Find issues not updated in at least N days, most stale first. Each
    result carries a stale_days field. There is no label for staleness, so
    this is the only way to answer questions about neglect, rot, or things
    that have gone quiet. Combine it with labels to ask sharper questions -
    for example stale blocked issues, where the blocker itself may no longer
    be current.

    Args:
        days: Minimum days since last update. Defaults to 30.
        state: One of open, closed, all. Defaults to open.
        labels: Optional comma-separated label filter.
    """
    return github_tools.find_stale_issues(
        days=days, state=state, labels=labels or None
    )


@mcp.tool()
def get_issue_comments(issue_number: int) -> list:
    """Fetch the full comment thread on a single issue by number. Use this
    when the issue title and truncated body do not explain the situation -
    for example to find out WHY something is blocked, or what the latest
    status is. Costs one API call per issue, so call it only on issues that
    matter to the question.

    Args:
        issue_number: The issue number, e.g. 7.
    """
    return github_tools.get_issue_comments(issue_number)


if __name__ == "__main__":
    mcp.run()
