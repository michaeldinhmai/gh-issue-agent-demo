# gh-issue-agent

A small agentic loop: you ask a question in English about a GitHub backlog, and Claude decides which GitHub Issues API calls to make to answer it.

```
$ python agent.py "what's blocked right now and why"
```

There is no keyword matching, no routing table, and no `if "blocked" in question` anywhere in the code. The program's only job is to describe two tools, execute whatever the model asks for, and hand the results back. The decision about *which* tool to call, with *which* arguments, and *how many times*, belongs entirely to the model.

The repository this agent reads is this repository. The issues in the Issues tab are **synthetic seed data** invented for the demo — a fictional billing-reconciliation service — so anyone can clone this and see the agent produce the same answers.

---

## Why this exists

Most "AI-powered" tooling is a single prompt with a single response. An agent is different in one specific way: the model is given the ability to act, observe the result, and decide what to do next. That loop is the entire distinction, and it is about forty lines of Python. This project is those forty lines, with nothing else in the way.

---

## The two tools

| Tool | Backed by | Returns |
|---|---|---|
| `list_issues(state, labels, assignee)` | `GET /repos/{owner}/{repo}/issues` | Number, title, state, labels, assignee, comment count, timestamps, truncated body |
| `get_issue_comments(issue_number)` | `GET /repos/{owner}/{repo}/issues/{n}/comments` | Author, timestamp, and body for each comment |

Both are ordinary Python functions calling the GitHub REST API with `requests`. Neither knows anything about Claude.

---

## How the agent decides which tool to call

The model never sees the Python. It sees the JSON schemas in `TOOLS` — a name, a description, and a typed parameter list per tool. Those descriptions are the entire basis for its routing decision, which makes them prompt engineering rather than documentation. Two choices in this project do most of the work:

- `list_issues` is described as the broad first step, and its return value is documented as including a **comment count**. That gives the model a cheap signal for deciding whether a thread is worth fetching.
- `get_issue_comments` is described as the tool for finding out *why* — and explicitly as costing one API call per issue. That discourages fetching all twelve threads when three will do.

The seed data is arranged to make the decision visible. Issue #1 is labeled `blocked`, but its body never says what is blocking it — the answer ("vendor ticket VS-4471") lives only in the comments. So a question like *"what's blocked right now and why"* cannot be answered from `list_issues` alone. Watching the trace, you see the model call `list_issues(labels="blocked")`, get three issues back, and then decide on its own to call `get_issue_comments` on each of them. Nothing in the code told it to do that.

Ask a question that *is* answerable from the list — *"what has nobody picked up yet"* — and it makes one call and stops. Same code, different plan.

---

## How the loop works

```
question ──> messages.create(tools=[...])
                     │
                     ├─ stop_reason == "tool_use"  ──> run the functions
                     │                                  append results
                     │                                  loop  ─────┐
                     │                                             │
                     └─ stop_reason == "end_turn"  ──> final text  │
                             ▲                                     │
                             └─────────────────────────────────────┘
```

Step by step, in `ask()`:

1. **Send.** Post the conversation to `POST /v1/messages` along with the tool schemas. Tools are a parameter on the ordinary Messages endpoint; there is no separate "agent API."
2. **Branch on `stop_reason`.** `"tool_use"` means the response contains one or more `tool_use` blocks and the model is waiting on you. `"end_turn"` means it is finished and the loop exits.
3. **Append the assistant turn verbatim.** `messages.append({"role": "assistant", "content": response.content})` — the whole content list, not just the text. The API is stateless: this list is the only memory the loop has, and a dropped block breaks the next request.
4. **Execute.** Each `tool_use` block carries a `name`, an `input` dict, and an `id`. Dispatch through `TOOL_FUNCTIONS` and capture the output. Tool inputs are parsed JSON — never string-match the serialized form.
5. **Return the results.** Each result is a `tool_result` block whose `tool_use_id` matches the originating call. **All results from one assistant turn go back in a single user message.** Splitting them across several messages trains the model to stop issuing parallel calls.
6. **Repeat.** The model now sees its own request and the real data, and decides whether it has enough to answer.

Two details that matter more than they look:

- **A failed tool returns a result, not an exception.** `run_tool` catches everything and returns the error text with `is_error: True`. The model sees the failure and can retry with different arguments; an uncaught exception would just kill the loop.
- **Tool results are projected down before they are returned.** GitHub sends roughly eighty fields per issue. `list_issues` keeps nine and truncates the body to 600 characters. Every field you keep is re-sent on every subsequent turn of the loop, so the projection is a cost decision, not tidiness.

`max_turns` caps the loop at 10 iterations so a pathological plan cannot bill indefinitely.

---

## Setup

Requires Python 3.10+ and, optionally, the [GitHub CLI](https://cli.github.com/).

```bash
git clone https://github.com/michaeldinhmai/gh-issue-agent-demo.git
cd gh-issue-agent-demo
python -m venv .venv
.venv/Scripts/activate      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then fill in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=                 # optional — falls back to `gh auth token`
GITHUB_REPO=michaeldinhmai/gh-issue-agent-demo
```

`ANTHROPIC_API_KEY` comes from [console.anthropic.com](https://console.anthropic.com/settings/keys). `GITHUB_TOKEN` only needs read access to issues; leave it blank and the agent borrows the token from an authenticated `gh` CLI so no PAT has to be written to disk. `.env` is gitignored.

## Running it

```bash
python agent.py "what's blocked right now and why"
python agent.py "summarize the open bugs"
python agent.py "what has nobody picked up yet"
```

Each tool call is printed as it happens — `-> tool(args)` for the request, `<- result` for the response — so the plan is visible rather than inferred. Set `AGENT_VERBOSE=0` to silence the trace and print only the answer.

---

## Design notes

**Manual loop, not the SDK tool runner.** The Anthropic Python SDK ships `client.beta.messages.tool_runner`, which drives this loop for you from decorated functions. It is the right choice for production. The loop is written out by hand here because the loop is the thing being demonstrated — and because a hand-written loop avoids the beta dependency and leaves room for per-turn control (approval gates, budget tracking, logging) that the runner exposes only through hooks.

**Model.** `claude-opus-5`. Thinking is on by default on this model, so the model plans before committing to a call; the thinking blocks come back in `response.content` and are echoed to the API unchanged, which is why the assistant turn is appended whole.

**`strict: True` on `get_issue_comments` only — and that asymmetry was earned.** Strict schema validation guarantees the `input` dict matches the schema, so `TOOL_FUNCTIONS[name](**tool_input)` can't blow up on an unexpected keyword. It was originally set on both tools. With it on `list_issues` — which has three *optional* parameters and `required: []` — roughly a third of calls came back with a corrupted `assignee` value: the model wanted `list_issues(labels="bug", state="open")`, had no use for `assignee`, and emitted fragments of its own tool-call markup into that string rather than omitting the field. GitHub answered 422.

Strict validation can't catch it, because the garbage *is* a valid string. Dropping strict from `list_issues` — where every parameter is optional and the model needs the freedom to omit fields — eliminated it; `get_issue_comments` has one required parameter and never exhibited the problem, so it keeps strict. The rule of thumb: strict pairs well with required parameters and badly with a bag of optional ones.

Worth noting how the loop behaved while the bug was live: the 422 came back as a `tool_result` with `is_error: True`, the model read the failure, retried with different arguments, and still produced a correct answer. The run was slower and cost more, but it did not fail. That's the practical argument for returning tool errors as results instead of raising.

**Read-only by design.** Neither tool writes. An agent that can close issues is a different risk conversation, and not one this demo needs to have.

---

## Files

| File | Purpose |
|---|---|
| `agent.py` | Tool implementations, tool schemas, and the loop |
| `requirements.txt` | `anthropic`, `requests`, `python-dotenv` |
| `.env.example` | Template for credentials |

