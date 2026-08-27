"""A minimal agentic loop: Claude answers questions about a GitHub backlog by
choosing which GitHub Issues API calls to make.

This file owns nothing but the loop. The tools it drives live in
github_tools.py and know nothing about Claude - which is what lets
mcp_server.py hand the same tools to Claude Desktop instead, with the host
supplying the loop. Run with:

    python agent.py "what's blocked right now and why"
"""

import json
import os
import sys

import anthropic

from github_tools import REPO, TOOLS, TOOL_FUNCTIONS  # noqa: F401

# Claude writes em-dashes and arrows; the Windows console defaults to cp1252
# and would mangle them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MODEL = "claude-opus-5"
VERBOSE = os.environ.get("AGENT_VERBOSE", "1") != "0"


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
