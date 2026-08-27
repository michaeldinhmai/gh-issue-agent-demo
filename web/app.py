"""A web UI that streams the agent's plan as it forms.

The point of this UI is not the answer - it is the trace. A chat box that
returns a paragraph looks like every other LLM demo and proves nothing.
Watching the tool calls appear one at a time, with their arguments, is what
makes the loop legible.

It reuses ghagent.loop.run() unchanged: the loop already yields one event per
step, so the server's whole job is to forward those events as Server-Sent
Events. No agent logic lives in this file.

    uvicorn web.app:app --reload --port 8000
"""

import asyncio
import json
import pathlib

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse

from ghagent.config import REPO
from ghagent.loop import run

app = FastAPI(title="gh-issue-agent")
STATIC = pathlib.Path(__file__).parent / "static"

SUGGESTIONS = [
    "What's blocked right now and why?",
    "Summarize the open bugs",
    "What has nobody picked up yet?",
    "What's gone stale and nobody is chasing?",
]


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    return {"repo": REPO, "suggestions": SUGGESTIONS}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


@app.get("/api/ask")
async def ask(q: str, max_turns: int = 10):
    """Stream the loop's events to the browser as they happen.

    run() is a synchronous generator that blocks on the Anthropic API, so it
    is stepped in a worker thread. Doing it inline would stall the event loop
    and the browser would receive everything at once at the end - which would
    defeat the entire purpose of this page.
    """

    async def stream():
        loop = asyncio.get_running_loop()
        events = run(q, max_turns=max_turns)
        sentinel = object()

        while True:
            event = await loop.run_in_executor(None, next, events, sentinel)
            if event is sentinel:
                break
            yield _sse(event)
            if event["type"] in ("answer", "stopped"):
                break

        yield _sse({"type": "done"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
