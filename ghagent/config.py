"""Credentials, target repo, and the clock. Everything environment-shaped."""

import datetime
import os
import pathlib
import subprocess

from dotenv import load_dotenv

# Anchored to the package, not the process working directory. An MCP host
# launches the server from wherever it likes, and a bare load_dotenv() would
# silently find nothing.
load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

GITHUB_API = "https://api.github.com"
DEFAULT_REPO = "michaeldinhmai/gh-issue-agent-demo"
MODEL = "claude-opus-5"

REPO = os.environ.get("GITHUB_REPO", "").strip() or DEFAULT_REPO


def github_token():
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


GITHUB_TOKEN = github_token()


def utcnow():
    """Current UTC time, overridable via AGENT_NOW for tests and demos.

    An injected clock is the only way to test time-dependent logic without
    sleeping. It doubles as a demo affordance: every seed issue in this repo
    was created in the same minute, so `AGENT_NOW=2026-11-01` is what makes
    find_stale_issues return anything interesting.
    """
    override = os.environ.get("AGENT_NOW", "").strip()
    if override:
        stamp = datetime.datetime.fromisoformat(override)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return stamp
    return datetime.datetime.now(datetime.timezone.utc)
