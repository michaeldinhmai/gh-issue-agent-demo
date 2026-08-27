"""The agentic loop.

`run()` is a generator that yields an event per step, rather than printing.
That one choice is why the CLI, the web UI, and the evals can all share a
single implementation: the CLI prints the events, the web UI streams them to
a browser, and the evals assert on them.

`ask()` wraps `run()` for callers that only want the final text.
"""

import json

import anthropic

from ghagent.config import MODEL
from ghagent.tools import TOOL_FUNCTIONS, TOOLS

SYSTEM_PROMPT_TEMPLATE = (
    "You are a project analyst for the GitHub repository {repo}. Answer "
    "questions about the backlog using only the tools provided; never invent "
    "issue numbers, labels, or assignees. Plan your calls: start broad with "
    "list_issues, then pull comment threads only where they change the "
    "answer. Cite issues as #<number> so the reader can look them up. Be "
    "concise and lead with the answer, not with a description of your process."
)


def system_prompt(repo=None):
    from ghagent.config import REPO

    return SYSTEM_PROMPT_TEMPLATE.format(repo=repo or REPO)


def run_tool(name, tool_input):
    """Dispatch one tool_use block to the matching Python function.

    Returns (content, is_error). A failure becomes a value, not an exception,
    so the model can read it and retry instead of the loop dying.
    """
    try:
        return json.dumps(TOOL_FUNCTIONS[name](**tool_input)), False
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}", True


def run(question, max_turns=10, client=None):
    """Drive the loop, yielding an event per step.

    Events: {"type": "tool_call"|"tool_result"|"answer"|"stopped", ...}
    """
    client = client or anthropic.Anthropic()
    messages = [{"role": "user", "content": question}]

    for turn in range(1, max_turns + 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=system_prompt(),
            tools=TOOLS,
            messages=messages,
        )

        # The assistant turn goes back verbatim - tool_use and thinking blocks
        # included. The API is stateless; this list is the loop's only memory.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            yield {
                "type": "answer",
                "turn": turn,
                "text": "\n".join(
                    b.text for b in response.content if b.type == "text"
                ),
                "stop_reason": response.stop_reason,
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
            }
            return

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            yield {"type": "tool_call", "turn": turn, "id": block.id,
                   "tool": block.name, "input": block.input}

            content, is_error = run_tool(block.name, block.input)

            yield {"type": "tool_result", "turn": turn, "id": block.id,
                   "tool": block.name, "content": content, "is_error": is_error}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
                "is_error": is_error,
            })

        # All results from one assistant turn go back in a SINGLE user message.
        # Splitting them across messages teaches the model to stop issuing
        # parallel tool calls.
        messages.append({"role": "user", "content": tool_results})

    yield {"type": "stopped", "turn": max_turns,
           "text": "Stopped: hit the max_turns limit without a final answer."}


def ask(question, max_turns=10, client=None, trace=None):
    """Run the loop and return just the final text.

    Pass a list as `trace` to collect the tool calls - that record, not the
    prose, is what the evals assert on.
    """
    answer = ""
    pending = {}
    for event in run(question, max_turns=max_turns, client=client):
        if event["type"] == "tool_call" and trace is not None:
            entry = {"turn": event["turn"], "tool": event["tool"],
                     "input": event["input"], "is_error": False}
            trace.append(entry)
            pending[event["id"]] = entry
        elif event["type"] == "tool_result" and trace is not None:
            pending.pop(event["id"], {})["is_error"] = event["is_error"]
        elif event["type"] in ("answer", "stopped"):
            answer = event["text"]
    return answer
