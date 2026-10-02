"""
POST /agent — web_agent: a natural-language task drives a stealth Chromium.

    { task, start_url?, max_steps?, allowed_domains?, output_schema?, timeout_ms?,
      stealth?, allow_mutations?, allow_form_submit? }
  → { final_result, success, steps:[{n, action, url}], urls, duration_s,
      blocked_requests, form_submissions, allowed_domains, stealth, error }

This module is only the HTTP edge. The run itself is agent_runner.py, executed
by a SEPARATE interpreter (AGENT_PYTHON, default /opt/agent-venv/bin/python)
because browser-use cannot share this venv with scrapling[fetchers]==0.4.14
(hard anyio and markdownify pin conflicts — see requirements-agent.txt). Nothing
here imports browser-use, so a broken agent venv cannot take /fetch down.

Bounds, all enforced here or in the runner:
- max_steps   default 15, max 40 (browser-use's own step cap).
- timeout_ms  default 180 s, max 300 s. The runner stops itself at the deadline
              and returns a partial result; this process kills the runner's
              whole process group (Chromium too) at deadline + KILL_SLACK_S.
              The toolkit client aborts at deadline + 25 s, after both.
- domains     allowed_domains, or the start_url's host when omitted. A run with
              neither is refused: an agent with no fence is not offered.
- one run per replica: a non-blocking flock on AGENT_LOCK_PATH, so a second
              uvicorn worker cannot start a second agent either. A busy replica
              answers 429 at once instead of queueing a caller for minutes.
              It never touches the /fetch executors: the run is another process.

Mutations and the form bridge: see agent_runner.py. In short, the agent's own
browser cannot submit forms (non-GET documents and same-domain non-GET
xhr/fetch are aborted) unless allow_mutations=true; with allow_form_submit=true
it gets ONE call to submit_form_via_web_tools, which goes through Camoufox
/form-submit and its single-POST guarantee.

Logs: one line per run with the outcome, step count and duration. Never the
task, field values, the LLM key or query strings.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("scrapling-svc.agent")

router = APIRouter()

RUNNER = Path(__file__).with_name("agent_runner.py")
KILL_SLACK_S = 15
MAX_STEPS = 40
MAX_TIMEOUT_MS = 300_000

# Env the runner needs, and nothing else from this process: the sidecar's env is
# not the agent's business. The LLM key reaches the runner through env, never
# argv (argv is world-readable in /proc).
_PASS_ENV = (
    "PATH", "HOME", "LANG", "TMPDIR", "PLAYWRIGHT_BROWSERS_PATH",
    "AGENT_LLM_PROVIDER", "AGENT_LLM_MODEL", "AGENT_LLM_API_KEY", "AGENT_LLM_BASE_URL",
    "AGENT_PROFILE_DIR", "PROXY_URL", "CAMOUFOX_URL",
)
# Providers that run without a key (a local model server; the offline test script).
_KEYLESS_PROVIDERS = ("ollama",)


class AgentRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=4000)
    start_url: str | None = Field(None, description="Opened before the first step")
    max_steps: int = Field(15, ge=1, le=MAX_STEPS)
    allowed_domains: list[str] | None = Field(
        None, max_length=20,
        description="Hosts the agent may navigate (subdomains included). Default: start_url's host",
    )
    output_schema: dict[str, Any] | None = Field(
        None, description="JSON schema (type=object) the final_result must match"
    )
    timeout_ms: int = Field(180_000, ge=10_000, le=MAX_TIMEOUT_MS)
    stealth: bool = Field(False, description="Residential egress (PROXY_URL); default direct")
    allow_mutations: bool = Field(
        False, description="Let the agent's own browser send non-GET requests (form posts)"
    )
    allow_form_submit: bool = Field(
        False, description="Give the agent ONE submit_form_via_web_tools call (Camoufox /form-submit)"
    )


def agent_disabled_reason() -> str | None:
    provider = os.environ.get("AGENT_LLM_PROVIDER", "anthropic").lower()
    if provider in _KEYLESS_PROVIDERS:
        return None
    if not os.environ.get("AGENT_LLM_API_KEY"):
        return "set AGENT_LLM_API_KEY"
    return None


def _agent_python() -> str:
    return os.environ.get("AGENT_PYTHON", "/opt/agent-venv/bin/python")


class _ReplicaLock:
    """Non-blocking exclusive flock: one agent run per container, across workers."""

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> bool:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


_started_at: float | None = None


def agent_health() -> dict:
    """For /healthz: whether the tool is enabled and how long the current run has been going."""
    return {
        "disabled": agent_disabled_reason(),
        "busy_age_s": round(time.monotonic() - _started_at, 1) if _started_at else None,
    }


async def _run_subprocess(payload: dict, deadline_s: float) -> dict:
    env = {k: os.environ[k] for k in _PASS_ENV if k in os.environ}
    proc = await asyncio.create_subprocess_exec(
        _agent_python(), str(RUNNER),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        # The runner's stderr carries browser-use's logging, which includes the
        # task and typed text. Dropped on purpose — see the module docstring.
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd=str(RUNNER.parent),
        start_new_session=True,  # own process group, so the kill reaches Chromium
    )
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(payload).encode()), timeout=deadline_s + KILL_SLACK_S
        )
    except asyncio.TimeoutError:
        _kill_group(proc)
        await proc.wait()
        raise HTTPException(
            status_code=504,
            detail=f"agent run exceeded {deadline_s + KILL_SLACK_S:.0f}s and was killed",
        )
    finally:
        # A clean exit can still orphan Chromium helpers; reap the group either way.
        _kill_group(proc)
    line = (out or b"").decode(errors="replace").strip().splitlines()
    if not line:
        raise HTTPException(status_code=502, detail=f"agent runner exited {proc.returncode} with no result")
    try:
        return json.loads(line[-1])
    except ValueError:
        raise HTTPException(status_code=502, detail="agent runner returned malformed output")


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


@router.get("/agent/healthz")
def agent_healthz():
    """Enabled? Runner interpreter present? Busy for how long? (No browser is launched.)"""
    return {**agent_health(), "runner_python": os.path.exists(_agent_python())}


@router.post("/agent")
async def agent_endpoint(req: AgentRequest):
    global _started_at

    reason = agent_disabled_reason()
    if reason:
        return JSONResponse(status_code=503, content={"disabled": True, "reason": reason})
    if not req.start_url and not req.allowed_domains:
        raise HTTPException(status_code=400, detail="start_url or allowed_domains is required")
    if req.output_schema is not None and (
        req.output_schema.get("type") != "object" or not req.output_schema.get("properties")
    ):
        raise HTTPException(status_code=400, detail="output_schema must be a JSON schema with type=object and properties")
    if req.stealth and not os.environ.get("PROXY_URL"):
        raise HTTPException(status_code=400, detail="stealth=true needs PROXY_URL on the sidecar")

    lock = _ReplicaLock(os.environ.get("AGENT_LOCK_PATH", "/tmp/web-agent.lock"))
    if not lock.acquire():
        return JSONResponse(
            status_code=429,
            content={"busy": True, "retryable": True, "reason": "an agent run is already in progress on this replica"},
        )
    _started_at = time.monotonic()
    try:
        data = await _run_subprocess(req.model_dump(), req.timeout_ms / 1000)
    finally:
        _started_at = None
        lock.release()

    if not data.get("ok"):
        err = str(data.get("error", "unknown"))
        log.warning("agent failed: %s", err[:200])
        status = 400 if err.startswith("ValueError") else 502
        raise HTTPException(status_code=status, detail=err)
    result = data["result"]
    log.info(
        "agent done success=%s steps=%d duration_s=%s blocked=%d forms=%d stealth=%s error=%s",
        result.get("success"), len(result.get("steps", [])), result.get("duration_s"),
        len(result.get("blocked_requests", [])), len(result.get("form_submissions", [])),
        result.get("stealth"), (result.get("error") or "")[:80],
    )
    return result
