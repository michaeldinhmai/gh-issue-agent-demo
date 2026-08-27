"""Tests for the agentic loop's message protocol.

These are the highest-value tests in the project. The loop's contract with
the API is easy to break in ways that produce no error - a dropped content
block, a mismatched tool_use_id, tool results split across two user messages.
The API accepts most of that and the model just gets quietly worse.

A fake client makes the protocol assertable without spending a token.
"""

import json
import types

import pytest

import agent


# --------------------------------------------------------------------------
# A minimal stand-in for the Anthropic client.
# --------------------------------------------------------------------------

def text_block(text):
    return types.SimpleNamespace(type="text", text=text)


def thinking_block(text=""):
    """Opus 5 thinks by default; these blocks come back in content and must
    be echoed to the API unchanged."""
    return types.SimpleNamespace(type="thinking", thinking=text)


def tool_use_block(id, name, input):
    return types.SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def response(content, stop_reason):
    return types.SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=types.SimpleNamespace(input_tokens=100, output_tokens=10),
    )


class FakeClient:
    """Replays a scripted list of responses and records what it was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        # Snapshot the message list as it was at call time.
        self.requests.append(json.loads(json.dumps(
            kwargs["messages"], default=lambda o: {"__block__": o.type}
        )))
        return self.script.pop(0)


@pytest.fixture(autouse=True)
def stub_tools(monkeypatch):
    """Tools return canned data; these tests are about the loop, not GitHub."""
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "list_issues", lambda **_: [{"number": 1}])
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_issue_comments",
                        lambda **_: [{"author": "someone", "body": "because"}])
    monkeypatch.setattr(agent, "VERBOSE", False)


# --------------------------------------------------------------------------

def test_loop_exits_on_end_turn_and_returns_the_text():
    client = FakeClient([response([text_block("Three issues are blocked.")], "end_turn")])

    answer = agent.ask("what's blocked", client=client)

    assert answer == "Three issues are blocked."
    assert len(client.requests) == 1


def test_loop_continues_while_stop_reason_is_tool_use():
    client = FakeClient([
        response([tool_use_block("t1", "list_issues", {})], "tool_use"),
        response([text_block("done")], "end_turn"),
    ])

    agent.ask("q", client=client)

    assert len(client.requests) == 2


def test_all_tool_results_from_one_turn_go_in_a_single_user_message():
    """Splitting results across messages teaches the model to stop making
    parallel calls. This is the regression this test exists to catch."""
    client = FakeClient([
        response([
            tool_use_block("t1", "get_issue_comments", {"issue_number": 1}),
            tool_use_block("t2", "get_issue_comments", {"issue_number": 5}),
            tool_use_block("t3", "get_issue_comments", {"issue_number": 8}),
        ], "tool_use"),
        response([text_block("done")], "end_turn"),
    ])

    agent.ask("q", client=client)

    second_request = client.requests[1]
    user_messages = [m for m in second_request if m["role"] == "user"]

    # One for the question, exactly one carrying all three results.
    assert len(user_messages) == 2
    assert len(user_messages[1]["content"]) == 3


def test_every_tool_result_id_matches_its_tool_use_id():
    client = FakeClient([
        response([
            tool_use_block("call_abc", "list_issues", {}),
            tool_use_block("call_xyz", "get_issue_comments", {"issue_number": 1}),
        ], "tool_use"),
        response([text_block("done")], "end_turn"),
    ])

    agent.ask("q", client=client)

    results = client.requests[1][-1]["content"]

    assert [r["tool_use_id"] for r in results] == ["call_abc", "call_xyz"]
    assert all(r["type"] == "tool_result" for r in results)


def test_assistant_turn_is_appended_whole_including_thinking_blocks():
    """The API is stateless - the message list is the loop's only memory.
    Extracting just the text would drop thinking and tool_use blocks and
    break the next request."""
    blocks = [
        thinking_block("plan: filter by label first"),
        text_block("Checking the blocked label."),
        tool_use_block("t1", "list_issues", {"labels": "blocked"}),
    ]
    client = FakeClient([
        response(blocks, "tool_use"),
        response([text_block("done")], "end_turn"),
    ])

    agent.ask("q", client=client)

    assistant = [m for m in client.requests[1] if m["role"] == "assistant"][0]

    assert len(assistant["content"]) == 3
    assert [b["__block__"] for b in assistant["content"]] == [
        "thinking", "text", "tool_use",
    ]


def test_a_failing_tool_is_reported_back_instead_of_killing_the_loop(monkeypatch):
    """This is what kept the agent answering correctly while the strict-schema
    bug was producing GitHub 422s."""
    def boom(**_):
        raise RuntimeError("422 Unprocessable Entity")

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "list_issues", boom)
    client = FakeClient([
        response([tool_use_block("t1", "list_issues", {"assignee": "garbage"})], "tool_use"),
        response([text_block("recovered")], "end_turn"),
    ])

    answer = agent.ask("q", client=client)

    result = client.requests[1][-1]["content"][0]
    assert result["is_error"] is True
    assert "422" in result["content"]
    assert answer == "recovered"


def test_max_turns_caps_a_runaway_plan():
    """Without the cap, a model that never stops calling tools bills forever."""
    client = FakeClient([
        response([tool_use_block(f"t{i}", "list_issues", {})], "tool_use")
        for i in range(5)
    ])

    answer = agent.ask("q", max_turns=3, client=client)

    assert "max_turns" in answer
    assert len(client.requests) == 3


def test_every_declared_tool_has_an_implementation():
    """A schema with no function behind it fails only at runtime, mid-answer."""
    declared = {tool["name"] for tool in agent.TOOLS}

    assert declared == set(agent.TOOL_FUNCTIONS)


def test_no_all_optional_schema_declares_strict():
    """The bug this project actually hit: strict: True on a schema whose
    parameters are all optional pushed the model to emit values for fields it
    wanted to omit, corrupting them. Guard the rule, not just the instance."""
    for tool in agent.TOOLS:
        schema = tool["input_schema"]
        all_optional = not schema.get("required")
        assert not (all_optional and tool.get("strict")), tool["name"]
