"""Behavioural evals: does the model choose the right tools?

Unit tests cannot answer this. Tool selection is a model decision, it is
non-deterministic, and the thing that breaks it is usually a reworded tool
description rather than a code change - which no unit test would notice.

So these assert on the *trace* (which tools were called, with what shape of
argument), never on the prose. Prose assertions are brittle and would fail on
a rewrite that is equally correct.

Unlike the unit tests, this spends tokens and needs a live GITHUB_REPO.

    python evals/eval_routing.py            # one pass
    python evals/eval_routing.py --runs 3   # three passes, for a pass rate
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402


def tools_used(trace):
    return {call["tool"] for call in trace}


CASES = [
    {
        "name": "why-blocked requires chaining into comments",
        "question": "what's blocked right now and why",
        # The 'why' lives only in comment threads. Answering from list_issues
        # alone means the model made something up.
        "check": lambda trace: "get_issue_comments" in tools_used(trace),
    },
    {
        "name": "why-blocked narrows before fetching threads",
        "question": "what's blocked right now and why",
        # It should not pull every thread in the repo; blocked issues are a
        # small subset. More than 5 comment fetches means it stopped filtering.
        "check": lambda trace: sum(
            c["tool"] == "get_issue_comments" for c in trace) <= 5,
    },
    {
        "name": "unassigned question starts from list_issues",
        "question": "what has nobody picked up yet",
        # It reliably goes on to pull a few threads to separate 'unassigned
        # and pickable' from 'unassigned but blocked', which is a better
        # answer than the cheap one - so this only asserts the entry point.
        "check": lambda trace: trace and trace[0]["tool"] == "list_issues",
    },
    {
        "name": "staleness question routes to find_stale_issues",
        "question": "what has gone stale and nobody is chasing?",
        # There is no 'stale' label, so list_issues cannot answer this.
        "check": lambda trace: "find_stale_issues" in tools_used(trace),
    },
    {
        "name": "no tool call errors on a well-formed question",
        "question": "summarize the open bugs",
        # Catches malformed arguments - the class of bug that strict: True on
        # an all-optional schema used to produce.
        "check": lambda trace: not any(c["is_error"] for c in trace),
    },
]


def run_once():
    results = []
    for case in CASES:
        trace = []
        try:
            agent.ask(case["question"], trace=trace)
            passed = bool(case["check"](trace))
            detail = " -> ".join(c["tool"] for c in trace) or "(no tool calls)"
        except Exception as exc:
            passed, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((case["name"], passed, detail))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()

    tally = {case["name"]: 0 for case in CASES}

    for run in range(args.runs):
        print(f"\n=== run {run + 1}/{args.runs} ===")
        for name, passed, detail in run_once():
            tally[name] += passed
            print(f"  [{'PASS' if passed else 'FAIL'}] {name}\n         {detail}")

    print(f"\n=== pass rate over {args.runs} run(s) ===")
    failed_any = False
    for name, hits in tally.items():
        print(f"  {hits}/{args.runs}  {name}")
        failed_any |= hits < args.runs

    # Non-zero exit so this can gate CI, with the caveat that a single
    # failure on a stochastic system is a signal to investigate, not proof
    # of a regression. Look at the pass rate across runs.
    sys.exit(1 if failed_any else 0)


if __name__ == "__main__":
    main()
