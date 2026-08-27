"""Unit tests for the tool functions - the deterministic half of the agent.

No network, no API key, no tokens spent. Everything here is a pure function
or a projection over a fixed payload.
"""

import datetime

import pytest

import agent
import github_tools

UTC = datetime.timezone.utc


def raw_issue(**overrides):
    """A GitHub issue payload, trimmed to the fields the projection reads."""
    payload = {
        "number": 1,
        "title": "Duplicate payouts on webhook retry",
        "state": "open",
        "labels": [{"name": "bug"}, {"name": "blocked"}],
        "assignee": {"login": "michaeldinhmai"},
        "comments": 3,
        "created_at": "2026-08-26T21:27:10Z",
        "updated_at": "2026-08-26T21:27:33Z",
        "body": "Vendor retries the settlement webhook.",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def fake_gh(monkeypatch):
    """Replace the HTTP layer; capture the params each tool sends."""
    calls = []

    def _fake(path, params=None):
        calls.append((path, params))
        return _fake.payload

    _fake.payload = []
    _fake.calls = calls
    monkeypatch.setattr(github_tools, "_gh", _fake)
    return _fake


# --------------------------------------------------------------------------
# list_issues projection
# --------------------------------------------------------------------------

def test_projection_keeps_only_the_documented_fields(fake_gh):
    fake_gh.payload = [raw_issue()]

    issue = github_tools.list_issues()["issues"][0]

    assert set(issue) == {
        "number", "title", "state", "labels", "assignee",
        "comments", "created_at", "updated_at", "body",
    }
    assert issue["labels"] == ["bug", "blocked"]
    assert issue["assignee"] == "michaeldinhmai"


def test_pull_requests_are_dropped(fake_gh):
    """The issues endpoint also returns PRs. The model was not asked about PRs."""
    fake_gh.payload = [
        raw_issue(number=1),
        raw_issue(number=2, pull_request={"url": "https://api.github.com/..."}),
    ]

    assert [i["number"] for i in github_tools.list_issues()["issues"]] == [1]


def test_body_is_truncated_to_the_context_budget(fake_gh):
    fake_gh.payload = [raw_issue(body="x" * 5000)]

    assert len(github_tools.list_issues()["issues"][0]["body"]) == 600


def test_null_assignee_and_null_body_do_not_crash(fake_gh):
    fake_gh.payload = [raw_issue(assignee=None, body=None)]

    issue = github_tools.list_issues()["issues"][0]

    assert issue["assignee"] is None
    assert issue["body"] == ""


def test_filters_are_forwarded_but_empty_ones_are_omitted(fake_gh):
    fake_gh.payload = []

    github_tools.list_issues(state="all", labels="blocked")

    _, params = fake_gh.calls[0]
    assert params["state"] == "all"
    assert params["labels"] == "blocked"
    assert "assignee" not in params


# --------------------------------------------------------------------------
# Staleness math - the part most likely to be quietly wrong
# --------------------------------------------------------------------------

def test_days_since_counts_whole_days():
    now = datetime.datetime(2026, 9, 25, 0, 0, tzinfo=UTC)

    assert github_tools._days_since("2026-08-26T00:00:00Z", now) == 30


def test_days_since_does_not_round_up_a_partial_day():
    now = datetime.datetime(2026, 9, 25, 23, 59, tzinfo=UTC)

    assert github_tools._days_since("2026-08-26T00:00:00Z", now) == 30


def test_days_since_handles_naive_and_aware_without_mixing(monkeypatch):
    """GitHub sends Z-suffixed stamps; datetime refuses to subtract naive
    from aware, so a regression here raises rather than silently skewing."""
    monkeypatch.setenv("AGENT_NOW", "2026-11-01")

    assert github_tools._days_since("2026-08-26T00:00:00Z") == 67


def test_stale_threshold_is_inclusive(fake_gh, monkeypatch):
    monkeypatch.setenv("AGENT_NOW", "2026-09-25T00:00:00Z")
    fake_gh.payload = [raw_issue(updated_at="2026-08-26T00:00:00Z")]

    assert github_tools.find_stale_issues(days=30)["count"] == 1
    assert github_tools.find_stale_issues(days=31)["count"] == 0


def test_stale_issues_are_sorted_most_stale_first(fake_gh, monkeypatch):
    monkeypatch.setenv("AGENT_NOW", "2026-11-01T00:00:00Z")
    fake_gh.payload = [
        raw_issue(number=1, updated_at="2026-10-01T00:00:00Z"),   # 31 days
        raw_issue(number=2, updated_at="2026-08-01T00:00:00Z"),   # 92 days
        raw_issue(number=3, updated_at="2026-09-01T00:00:00Z"),   # 61 days
    ]

    result = github_tools.find_stale_issues(days=30)["issues"]

    assert [i["number"] for i in result] == [2, 3, 1]
    assert [i["stale_days"] for i in result] == [92, 61, 31]


def test_stale_days_is_added_without_losing_the_projection(fake_gh, monkeypatch):
    monkeypatch.setenv("AGENT_NOW", "2026-11-01T00:00:00Z")
    fake_gh.payload = [raw_issue()]

    issue = github_tools.find_stale_issues(days=1)["issues"][0]

    assert issue["stale_days"] == 66
    assert issue["title"] == "Duplicate payouts on webhook retry"


def test_clock_defaults_to_real_utc_when_unset(monkeypatch):
    monkeypatch.delenv("AGENT_NOW", raising=False)

    now = github_tools._now()

    assert now.tzinfo is not None
    assert abs((datetime.datetime.now(UTC) - now).total_seconds()) < 5


# --------------------------------------------------------------------------
# Error contract - a failing tool must return a result, never raise
# --------------------------------------------------------------------------

def test_tool_failure_is_returned_as_an_error_result(monkeypatch):
    def boom(**_):
        raise RuntimeError("422 Unprocessable Entity")

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "list_issues", boom)

    content, is_error = agent.run_tool("list_issues", {})

    assert is_error is True
    assert "422" in content


def test_unknown_tool_name_is_an_error_result_not_a_crash():
    content, is_error = agent.run_tool("delete_everything", {})

    assert is_error is True
    assert "KeyError" in content


def test_successful_tool_result_is_json(fake_gh):
    fake_gh.payload = [raw_issue()]

    content, is_error = agent.run_tool("list_issues", {"state": "open"})

    assert is_error is False
    assert content.startswith("{")


# --------------------------------------------------------------------------
# Envelopes - a bare [] cannot tell a model "nothing matched" apart from
# "the call failed", so every tool states what it looked for and how many it
# found.
# --------------------------------------------------------------------------

def test_empty_comment_thread_states_itself(fake_gh):
    fake_gh.payload = []

    result = github_tools.get_issue_comments(10)

    assert result == {"issue_number": 10, "comment_count": 0, "comments": []}


def test_no_matching_issues_echoes_the_filters_back(fake_gh):
    fake_gh.payload = []

    result = github_tools.list_issues(labels="nonexistent")

    assert result["count"] == 0
    assert result["filters"]["labels"] == "nonexistent"


def test_stale_result_states_the_clock_it_used(fake_gh, monkeypatch):
    """Without as_of, '0 days stale' is indistinguishable from a wrong clock."""
    monkeypatch.setenv("AGENT_NOW", "2026-11-01T00:00:00Z")
    fake_gh.payload = [raw_issue()]

    result = github_tools.find_stale_issues(days=1)

    assert result["as_of"].startswith("2026-11-01")
    assert result["threshold_days"] == 1
