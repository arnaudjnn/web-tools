"""
web_agent runner — one browser-use run, in its own process and its own venv.

Invoked by agent.py as `$AGENT_PYTHON agent_runner.py`, with the request as JSON
on stdin and the result as ONE JSON object on the original stdout. Everything
else (browser-use's own logging included) goes to stderr, which the parent
drops: browser-use logs the task text and typed values, and nothing a caller
typed may reach the sidecar's logs.

Why a separate process AND a separate venv
------------------------------------------
- browser-use 0.13.x cannot be installed next to scrapling[fetchers]==0.4.14:
  it pins anyio==4.12.1 where Scrapling needs anyio>=4.14.0, and it pins
  markdownify==1.2.2 where /markdown pins 1.2.3 (the web_fetch contract).
  Both are hard `==` pins upstream, so there is no common solution — see
  requirements-agent.txt for the resolver output.
- A process can be killed; a thread cannot. The parent enforces the wall-clock
  deadline by killing this process group (Chromium included), which is the
  only reliable stop for a wedged CDP call. That is the lesson of _execute's
  busy_age_s in app.py, not repeated here.

The browser
-----------
Patchright launches Chromium (new-headless "chromium" channel, persistent
user-data dir per egress) with a local CDP port, and browser-use connects to it
over `cdp_url`. Patchright's own connection stays open and owns two guards
browser-use knows nothing about:

  1. MUTATION GUARD (default on): any non-GET/HEAD/OPTIONS request of type
     `document` (a form submission) is aborted wherever it goes, and any
     non-GET xhr/fetch to an allowed (= the task's own) domain is aborted.
     `allow_mutations: true` turns this off. Forms are submitted through the
     `submit_form_via_web_tools` action instead (the FORM BRIDGE), which calls
     Camoufox /form-submit — the path that owns the single-POST guarantee.
  2. DOMAIN GUARD: a main-frame navigation outside allowed_domains is aborted
     at the network layer, in addition to browser-use's own allowed_domains
     check (which acts after the fact, on navigation events).

Stealth caveat, honestly: Patchright's patches are mostly in its own CDP
driver (no Runtime.enable, isolated worlds); browser-use drives the page over
its own CDP session and does not inherit them. What carries over is the launch
side — no --enable-automation, AutomationControlled off, a real Chromium build
rather than headless-shell — plus the persistent profile and the egress.
There is no CAPTCHA solving here and there will not be.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

# Before any browser_use import: its config is read from the environment, and the
# defaults phone home (PostHog telemetry, cloud sync) and download extensions.
for _k, _v in (
    ("ANONYMIZED_TELEMETRY", "false"),
    ("BROWSER_USE_CLOUD_SYNC", "false"),
    ("BROWSER_USE_DISABLE_EXTENSIONS", "1"),
    ("BROWSER_USE_LOGGING_LEVEL", "warning"),
):
    os.environ.setdefault(_k, _v)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Actions that reach outside the task (search engines, local files) or produce
# artefacts nobody collects. Removed from the action space entirely.
EXCLUDED_ACTIONS = ["search", "upload_file", "save_as_pdf"]
# The form bridge refuses to start a submission with less than this left on the
# run's deadline: killing the runner mid-submission leaves the outcome unknown.
FORM_MIN_BUDGET_S = 45


# ── pure helpers (unit-tested without a browser) ──────────────────────────────

def normalize_domains(allowed: list[str] | None, start_url: str | None) -> list[str]:
    """Bare hostnames, lower-case. Default: the start URL's host."""
    out: list[str] = []
    for d in allowed or []:
        d = d.strip().lower()
        if "://" in d:
            d = urlsplit(d).hostname or ""
        d = d.lstrip("*.").rstrip("/")
        if d and d not in out:
            out.append(d)
    if not out and start_url:
        host = (urlsplit(start_url).hostname or "").lower()
        if host:
            out.append(host[4:] if host.startswith("www.") else host)
    return out


def host_allowed(url: str, domains: list[str]) -> bool:
    """Exact host or any subdomain of an allowed domain. about:/data: pass."""
    parts = urlsplit(url)
    if parts.scheme in ("about", "data", "blob", "chrome", "devtools"):
        return True
    host = (parts.hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def is_blocked_mutation(method: str, resource_type: str, url: str, domains: list[str]) -> bool:
    """Would the mutation guard abort this request?

    document + unsafe method = a form submission (or a JS-built POST navigation):
    blocked to ANY host, because a form posting cross-origin is still a submission.
    xhr/fetch + unsafe method: blocked to the task's own domains only, so third-party
    beacons and analytics keep working. A site that READS through POST (GraphQL on
    its own origin) is blocked too — that is the documented price of the default;
    pass allow_mutations for it.
    """
    if method.upper() in SAFE_METHODS:
        return False
    if resource_type == "document":
        return True
    if resource_type in ("xhr", "fetch"):
        return host_allowed(url, domains)
    return False


def redact_url(url: str) -> str:
    """Scheme + host + path. Query strings carry tokens and typed values."""
    p = urlsplit(url)
    if not p.scheme or not p.netloc:
        return url[:200]
    return f"{p.scheme}://{p.netloc}{p.path}"[:300]


# Params whose values are safe to echo in step records. Text typed into inputs,
# JS code, file names and extraction queries are not.
_SAFE_PARAM_KEYS = ("index", "url", "new_tab", "down", "pages", "tab_id", "seconds", "success")


def describe_action(action: dict) -> str:
    """'click(index=3)' / 'navigate(url=https://x/y)' / 'input(index=2)'."""
    parts = []
    for name, params in action.items():
        if params is None:
            continue
        shown = []
        if isinstance(params, dict):
            for k in _SAFE_PARAM_KEYS:
                if k in params and params[k] is not None:
                    v = params[k]
                    if k == "url" and isinstance(v, str):
                        v = redact_url(v)
                    shown.append(f"{k}={v}")
        parts.append(f"{name}({', '.join(shown)})")
    return "; ".join(parts) or "unknown"


def html_excerpt(html: str, limit: int = 1500) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


# ── LLM ───────────────────────────────────────────────────────────────────────

def make_llm(provider: str, model: str, api_key: str, base_url: str | None):
    p = provider.lower()
    if p == "anthropic":
        from browser_use import ChatAnthropic

        class _AutoToolChoiceAnthropic(ChatAnthropic):
            # browser-use 0.13.10 forces tool_choice={"type":"tool"} for structured
            # output on every model it does not know. The 5.x models refuse that
            # (measured 2026-10-02 on claude-sonnet-5-5: HTTP 400 "tool_choice:
            # type tool and any are not supported for this model", every step),
            # and the library only switches to "auto" for fable/mythos by name.
            # "auto" works on every Claude model, so always use it.
            def _requires_auto_tool_choice(self) -> bool:  # noqa: D401
                return True

        # Cost of "auto": now and then the model calls the output tool with one
        # action's arguments flattened ({"index": 6} instead of {"action": [...]}).
        # browser-use rejects it and the next step recovers — measured 1 slip in
        # 4-5 steps on claude-sonnet-5-5, with or without adaptive thinking. That
        # is why max_steps defaults to 15 rather than to the few a task needs.
        return _AutoToolChoiceAnthropic(model=model, api_key=api_key, base_url=base_url, max_retries=2)
    if p in ("openai", "openai_compatible"):
        from browser_use import ChatOpenAI
        return ChatOpenAI(model=model, api_key=api_key, base_url=base_url)
    if p == "google":
        from browser_use import ChatGoogle
        return ChatGoogle(model=model, api_key=api_key, max_retries=2)
    if p == "openrouter":
        from browser_use import ChatOpenRouter
        return ChatOpenRouter(model=model, api_key=api_key, max_retries=2)
    if p == "groq":
        from browser_use import ChatGroq
        return ChatGroq(model=model, api_key=api_key, max_retries=2)
    if p == "ollama":
        from browser_use import ChatOllama
        return ChatOllama(model=model, host=base_url)
    if p == "scripted":
        # Offline smoke tests: replay a JSON list of AgentOutput dicts from a file
        # (api_key is the path). No network, no cost; see test_agent.py.
        return ScriptedLLM(json.loads(Path(api_key).read_text()))
    raise ValueError(f"unknown AGENT_LLM_PROVIDER {provider!r}")


class ScriptedLLM:
    """A browser-use chat model that replays canned AgentOutput steps.

    Each ainvoke pops the next step; when the script runs out it repeats the
    last one, which is how the step cap is exercised.
    """

    model = "scripted"
    _verified_api_keys = True

    def __init__(self, steps: list[dict]):
        self.steps = list(steps)
        self.calls = 0

    @property
    def provider(self) -> str:
        return "scripted"

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def model_name(self) -> str:
        return "scripted"

    async def ainvoke(self, messages, output_format=None, **kwargs):
        from browser_use.llm.views import ChatInvokeCompletion

        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        if output_format is None:
            return ChatInvokeCompletion(completion=json.dumps(step), usage=None)
        try:
            parsed = output_format.model_validate(step)
        except Exception:  # noqa: BLE001
            # On its last step browser-use narrows the action space to `done`;
            # a real model complies, so the script does too.
            parsed = output_format.model_validate(
                {**step, "action": [{"done": {"success": False, "text": "step cap reached"}}]}
            )
        return ChatInvokeCompletion(completion=parsed, usage=None)


# ── the run ───────────────────────────────────────────────────────────────────

def _proxy_from_url(url: str) -> dict | None:
    """Same parsing as app.py parse_proxy, for Playwright's proxy= dict."""
    m = re.match(r"^(https?)://([^:]+):([^@]+)@(.+)$", url or "")
    if not m:
        return None
    return {"server": f"{m.group(1)}://{m.group(4)}", "username": m.group(2), "password": m.group(3)}


def _clear_singleton_locks(user_data_dir: Path) -> None:
    # A killed run leaves Chromium's profile locks behind and the next launch
    # refuses the dir. Runs are serialised by agent.py's flock, so any lock
    # here is stale by construction.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"):
        try:
            (user_data_dir / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


async def _wait_devtools_port(user_data_dir: Path, timeout_s: float = 15.0) -> int:
    f = user_data_dir / "DevToolsActivePort"
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        try:
            first = f.read_text().splitlines()[0].strip()
            if first.isdigit():
                return int(first)
        except (FileNotFoundError, IndexError):
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("Chromium did not publish a DevTools port")


def build_form_bridge(tools, state: dict, domains: list[str], deadline: float, camoufox_url: str):
    """Register submit_form_via_web_tools on a browser-use Tools instance."""
    from browser_use.agent.views import ActionResult
    from pydantic import BaseModel, Field

    class BridgeField(BaseModel):
        selector: str = Field(..., description="CSS selector of the control")
        value: str | None = Field(None, description="text to type, or option value for action=select")
        action: Literal["type", "check", "select"] = "type"

    class SubmitFormParams(BaseModel):
        url: str = Field(..., description="URL of the page that holds the form")
        fields: list[BridgeField]
        submit: str = Field(..., description="CSS selector of the submit control")
        dismiss: list[str] = Field(default_factory=list, description="selectors clicked first (cookie walls)")
        success_url: str | None = Field(None, description="regex; a matching final URL means success")
        captcha_field: str | None = Field(None, description="POST field checked for token presence only")
        require_captcha_token: bool = False

    @tools.action(
        "Submit a form ONCE through web-tools' hardened form path (a separate stealth browser "
        "with a single-POST guarantee). This is the ONLY way to submit a form: clicking a "
        "submit button in your own browser is blocked. Map each field to a CSS selector you "
        "saw on the page. Allowed at most once per run; NEVER retry, whatever the result — "
        "a failure or lost response may still have submitted.",
        param_model=SubmitFormParams,
    )
    async def submit_form_via_web_tools(params: SubmitFormParams):
        import httpx

        if state["form_budget"] <= 0:
            return ActionResult(error="form already submitted this run; the single-POST rule forbids another attempt")
        if not host_allowed(params.url, domains):
            return ActionResult(error=f"{redact_url(params.url)} is outside allowed_domains")
        remaining = deadline - time.monotonic()
        if remaining < FORM_MIN_BUDGET_S:
            return ActionResult(error="not enough time left in this run to submit safely; not submitted")
        if not camoufox_url:
            return ActionResult(error="form bridge unavailable: CAMOUFOX_URL is not set on this sidecar")

        timeout_ms = int(min(240.0, remaining - 15) * 1000)
        body = {
            "url": params.url,
            "fields": [f.model_dump() for f in params.fields],
            "submit": params.submit,
            "dismiss": params.dismiss,
            "timeout_ms": timeout_ms,
            "fresh_ip": True,
            **({"success_url": params.success_url} if params.success_url else {}),
            **({"captcha_field": params.captcha_field} if params.captcha_field else {}),
            **({"require_captcha_token": True} if params.require_captcha_token else {}),
        }
        # Spend the budget BEFORE the call: a lost response may hide a POST.
        state["form_budget"] -= 1
        record: dict[str, Any] = {"url": redact_url(params.url), "fields": len(params.fields)}
        state["form_submissions"].append(record)
        try:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 30) as client:
                r = await client.post(camoufox_url.rstrip("/") + "/form-submit", json=body)
        except Exception as e:  # noqa: BLE001
            record.update(ok=False, outcome="unknown", error=f"transport: {type(e).__name__}")
            return ActionResult(
                error="form bridge lost the response; the outcome is UNKNOWN (it may have submitted). Do not retry.",
            )
        if r.status_code == 503 and '"retryable"' in r.text:
            # Camoufox proved zero POSTs left the browser: the budget comes back.
            state["form_budget"] += 1
            record.update(ok=False, outcome="not_submitted", status=503, error="retryable: zero POSTs sent")
            return ActionResult(error="form path was unavailable and sent nothing; you may call it once more")
        if r.status_code != 200:
            record.update(ok=False, outcome="unknown", status=r.status_code, error=r.text[:200])
            return ActionResult(error=f"form path HTTP {r.status_code}; the outcome is unknown. Do not retry.")
        data = r.json()
        summary = {
            "ok": data.get("ok"),
            "error": data.get("error"),
            "form_submissions": data.get("form_submissions"),
            "status": data.get("status"),
            "url": redact_url(data.get("url", "")),
            "page_text": html_excerpt(data.get("html", "")),
        }
        record.update(
            ok=bool(data.get("ok")), outcome="submitted" if data.get("form_submissions") else "not_submitted",
            status=data.get("status"), error=data.get("error"),
            form_submissions=data.get("form_submissions"),
        )
        return ActionResult(extracted_content=json.dumps(summary), long_term_memory=f"form submitted once: ok={summary['ok']}")

    return submit_form_via_web_tools


async def run_agent(req: dict, llm=None) -> dict:
    """Run one task. `llm` overrides the env-configured model (tests)."""
    from patchright.async_api import async_playwright
    from browser_use import Agent, BrowserSession, Tools

    t0 = time.monotonic()
    timeout_s = req["timeout_ms"] / 1000
    deadline = t0 + timeout_s
    max_steps = int(req["max_steps"])
    start_url = req.get("start_url")
    domains = normalize_domains(req.get("allowed_domains"), start_url)
    allow_mutations = bool(req.get("allow_mutations"))
    stealth = bool(req.get("stealth"))

    if llm is None:
        llm = make_llm(
            os.environ.get("AGENT_LLM_PROVIDER", "anthropic"),
            os.environ.get("AGENT_LLM_MODEL", "claude-sonnet-5-5"),
            os.environ.get("AGENT_LLM_API_KEY", ""),
            os.environ.get("AGENT_LLM_BASE_URL") or None,
        )

    proxy = None
    if stealth:
        proxy = _proxy_from_url(os.environ.get("PROXY_URL", ""))
        if proxy is None:
            raise ValueError("stealth=true needs PROXY_URL (http://user:pass@host:port) on this sidecar")

    profile_root = Path(os.environ.get("AGENT_PROFILE_DIR", "/tmp/web-agent-profile"))
    # One profile per egress: cookies earned on one IP must not travel to another.
    user_data_dir = profile_root / ("stealth" if stealth else "direct")
    user_data_dir.mkdir(parents=True, exist_ok=True)
    _clear_singleton_locks(user_data_dir)

    blocked: list[dict] = []
    state: dict[str, Any] = {"form_budget": 1, "form_submissions": []}

    async def guard(route, request):
        method, rtype, url = request.method, request.resource_type, request.url
        reason = None
        # The mutation rule first, and it reads nothing that can raise: it must
        # never fail open.
        if not allow_mutations and is_blocked_mutation(method, rtype, url, domains):
            reason = "mutation"
        elif rtype == "document" and not host_allowed(url, domains):
            try:
                # Main-frame navigations only; an off-fence iframe is a subresource.
                # request.frame raises for service-worker requests; those pass.
                if request.is_navigation_request() and request.frame.parent_frame is None:
                    reason = "domain"
            except Exception:  # noqa: BLE001
                pass
        try:
            if reason:
                blocked.append({"reason": reason, "method": method, "type": rtype, "url": redact_url(url)})
                await route.abort("blockedbyclient")
            else:
                await route.continue_()
        except Exception:  # noqa: BLE001 - the page went away mid-request
            pass

    steps: list[dict] = []
    history = None
    error: str | None = None
    agent = None

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(user_data_dir),
            channel="chromium",  # full Chromium in new-headless, not headless-shell
            headless=True,
            no_viewport=False,
            viewport={"width": 1280, "height": 900},
            args=["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"],
            **({"proxy": proxy} if proxy else {}),
        )
        try:
            await ctx.route("**/*", guard)
            port = await _wait_devtools_port(user_data_dir)

            session = BrowserSession(
                cdp_url=f"http://127.0.0.1:{port}",
                allowed_domains=[p for d in domains for p in (d, f"*.{d}")],
                keep_alive=True,  # Patchright owns the process; we close it below
                enable_default_extensions=False,
                captcha_solver=False,
            )
            tools = Tools(exclude_actions=EXCLUDED_ACTIONS)
            if req.get("allow_form_submit"):
                build_form_bridge(tools, state, domains, deadline, os.environ.get("CAMOUFOX_URL", ""))

            output_model = None
            if req.get("output_schema"):
                from browser_use.tools.extraction.schema_utils import schema_dict_to_pydantic_model
                output_model = schema_dict_to_pydantic_model(req["output_schema"])

            async def should_stop() -> bool:
                return time.monotonic() >= deadline - 5

            task = req["task"]
            if domains:
                task += f"\n\nStay on these domains: {', '.join(domains)}."
            if not allow_mutations:
                task += "\nForm submissions from your own browser are blocked by the network layer."
                if req.get("allow_form_submit"):
                    task += " To submit a form, call submit_form_via_web_tools exactly once."

            agent = Agent(
                task=task,
                llm=llm,
                browser_session=session,
                tools=tools,
                output_model_schema=output_model,
                initial_actions=[{"navigate": {"url": start_url, "new_tab": False}}] if start_url else None,
                directly_open_url=False,
                use_judge=False,
                enable_signal_handler=False,
                register_should_stop_callback=should_stop,
                max_failures=3,
                step_timeout=max(30, int(min(120, timeout_s))),
                llm_timeout=60,
                calculate_cost=False,
                file_system_path=str(profile_root / "fs"),
            )
            try:
                history = await asyncio.wait_for(agent.run(max_steps=max_steps), timeout=max(1.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                error = f"deadline: stopped after {timeout_s:.0f}s"
                history = agent.history
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {str(e)[:300]}"
                history = agent.history
            try:
                await session.kill()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass

    final_result: Any = None
    success = False
    urls: list[str] = []
    if history is not None:
        for i, item in enumerate(history.history, start=1):
            if item.model_output is None and not item.state.url:
                continue  # browser-use's synthetic "failed in max steps" tail record
            actions = [a.model_dump(exclude_unset=True) for a in item.model_output.action] if item.model_output else []
            steps.append({
                # step 0 = the start_url navigation (initial_actions), then 1..max_steps
                "n": item.metadata.step_number if item.metadata else i,
                "action": "; ".join(describe_action(a) for a in actions) if actions else "none",
                "url": redact_url(item.state.url or ""),
                **({"error": str(r.error)[:200]} if (r := next((r for r in item.result if r.error), None)) else {}),
            })
        urls = list(dict.fromkeys(redact_url(u) for u in history.urls() if u))
        final_result = history.final_result()
        if output_model is not None and final_result:
            try:
                final_result = json.loads(final_result)
            except (TypeError, ValueError):
                pass
        success = bool(history.is_done() and history.is_successful())
        if error is None and not history.is_done():
            error = f"max_steps: stopped after {max_steps} steps without finishing"

    return {
        "final_result": final_result,
        "success": success,
        "steps": steps,
        "urls": urls,
        "duration_s": round(time.monotonic() - t0, 2),
        "blocked_requests": blocked[:50],
        "form_submissions": state["form_submissions"],
        "allowed_domains": domains,
        "stealth": stealth,
        "error": error,
    }


def main() -> int:
    # The protocol channel is the ORIGINAL stdout; everything printed after this
    # (browser-use banners, logging handlers bound to sys.stdout) lands on stderr.
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        req = json.loads(sys.stdin.read())
        result = asyncio.run(run_agent(req))
        out.write(json.dumps({"ok": True, "result": result}) + "\n")
    except Exception as e:  # noqa: BLE001
        out.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {str(e)[:500]}"}) + "\n")
    out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
