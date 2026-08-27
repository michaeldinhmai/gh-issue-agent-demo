"""Terminal entry point. Prints the loop's events as they happen.

    gh-agent "what's blocked right now and why"
"""

import json
import os
import sys

from ghagent.loop import run

VERBOSE = os.environ.get("AGENT_VERBOSE", "1") != "0"


def render(event):
    """One line per event, so the plan is visible rather than inferred."""
    turn = event["turn"]
    if event["type"] == "tool_call":
        print(f"[turn {turn}] -> {event['tool']}({json.dumps(event['input'])})")
    elif event["type"] == "tool_result":
        body = event["content"]
        preview = body if len(body) < 160 else body[:160] + "..."
        flag = "ERROR " if event["is_error"] else ""
        print(f"[turn {turn}] <- {flag}{preview}")
    elif event["type"] == "answer":
        usage = event["usage"]
        print(f"\n[turn {turn}] stop_reason={event['stop_reason']} "
              f"| in={usage['input_tokens']} out={usage['output_tokens']}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        # Claude writes em-dashes; the Windows console defaults to cp1252.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    for event in run(" ".join(sys.argv[1:])):
        if VERBOSE:
            render(event)
        if event["type"] in ("answer", "stopped"):
            print(event["text"])


if __name__ == "__main__":
    main()
