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
# The file actions and screenshot write into a scratch dir nobody returns.
# (The grammar no longer limits this list: compact_action_envelope keeps the
# compiled grammar independent of how many actions there are.)
EXCLUDED_ACTIONS = [
    "search", "upload_file", "save_as_pdf",
    "write_file", "replace_file", "read_file", "screenshot",
]
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

# Keywords Anthropic structured outputs rejects or ignores; the SDK helpers strip
# them too. `default`/`title` are noise to the grammar compiler.
_UNSUPPORTED_SCHEMA_KEYS = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "minItems", "maxItems", "min_items", "max_items",
    "uniqueItems", "default", "title", "examples",
})


def to_structured_output_schema(node: Any) -> Any:
    """Make a pydantic/browser-use JSON schema acceptable to output_config.format.

    Every object gets additionalProperties:false (required by the API); numeric,
    string-length and array-size constraints are dropped (unsupported). Keys of
    a `properties` map are field names, not keywords, and are kept as they are.
    """
    if isinstance(node, list):
        return [to_structured_output_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if k in ("properties", "$defs", "definitions") and isinstance(v, dict):
            out[k] = {name: to_structured_output_schema(sub) for name, sub in v.items()}
        else:
            out[k] = to_structured_output_schema(v)
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
    return out


def compact_action_envelope(schema: dict) -> tuple[dict, str | None, dict]:
    """Shrink browser-use's AgentOutput schema to a grammar the API will compile.

    browser-use's `action` is an array of anyOf(one object per action), and every
    branch's parameters are part of the grammar. Measured 2026-10-02 on
    claude-sonnet-5-5: that compiles at 15 actions and is refused at 16 ("The
    compiled grammar is too large"), whatever is done to nullables or `required`.
    So the grammar keeps two item shapes:

      {"name": <enum of action names>, "params": [{"key": str, "value": str}]}
      the original, fully typed `done` branch (it carries the caller's output_schema)

    The envelope (the part the model used to flatten) and the final result are
    guaranteed by constrained decoding. Parameter names and types go to the model
    as text (the returned catalog, appended to the system prompt) and are coerced
    back by expand_action_envelope, then validated by browser-use's ActionModel.

    Why key/value strings and not one JSON string of params: a JSON object inside
    a JSON string double-escapes every quote, and JS for `evaluate` came back
    unterminated on 3 of 5 steps (measured). A plain string value is escaped once.

    Returns (schema, None, {}) unchanged for a schema that is not an AgentOutput.
    """
    action = schema.get("properties", {}).get("action")
    branches = (action or {}).get("items", {}).get("anyOf")
    if not branches:
        return schema, None, {}
    spec: dict[str, dict] = {}
    done_branch = None
    lines = []
    for b in branches:
        (name, params), = b["properties"].items()
        if name == "done":
            done_branch = b
            continue
        props = params.get("properties", {})
        spec[name] = props
        desc = params.get("description") or b.get("description") or ""
        args = ", ".join(
            f"{k}: {_type_label(v)}" + (f" ({v['description']})" if v.get("description") else "")
            for k, v in props.items()
        )
        lines.append(f"- {name}: {desc}\n    params: {args or 'none'}")
    generic = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": list(spec)},
            "params": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["key", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["name", "params"],
        "additionalProperties": False,
    }
    compact = json.loads(json.dumps(schema))
    compact["properties"]["action"] = {
        "type": "array",
        "description": action.get("description", "Actions to run in order"),
        # The last step's model (DoneAgentOutput) offers `done` alone: no generic branch.
        "items": {"anyOf": [b for b in (generic if spec else None, done_branch) if b]},
    }
    if len(compact["properties"]["action"]["items"]["anyOf"]) == 1:
        compact["properties"]["action"]["items"] = compact["properties"]["action"]["items"]["anyOf"][0]
    catalog = (
        "\n\n<available_actions>\nTo finish, emit the `done` action with its own fields. Every other "
        "action is {\"name\": <action>, \"params\": [{\"key\": <param>, \"value\": <string>}]}: one "
        "entry per parameter listed below, every listed parameter given. Values are strings: write "
        "numbers and booleans as JSON text (\"3\", \"true\"), \"null\" where null is allowed, and "
        "lists/objects as JSON text.\n" + "\n".join(lines) + "\n</available_actions>"
    )
    return compact, catalog, spec


def _type_label(prop: dict) -> str:
    if "anyOf" in prop:
        return " | ".join(_type_label(p) for p in prop["anyOf"])
    if "enum" in prop:
        return "one of " + "/".join(map(str, prop["enum"]))
    t = prop.get("type", "any")
    if t == "array":
        return f"list of {_type_label(prop.get('items', {}))}"
    return t if isinstance(t, str) else "/".join(t)


def _is_string_field(prop: dict) -> bool:
    if "anyOf" in prop:
        return any(_is_string_field(p) for p in prop["anyOf"])
    return prop.get("type") == "string"


def _coerce(prop: dict, value: str):
    if value.strip() == "null" and any(p.get("type") == "null" for p in prop.get("anyOf", [])):
        return None
    if _is_string_field(prop):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value  # browser-use's ActionModel reports the type error; the step retries


def expand_action_envelope(data: dict, spec: dict) -> dict:
    """Inverse of compact_action_envelope: back to browser-use's [{name: params}]."""
    acts = data.get("action")
    if not isinstance(acts, list):
        return data
    expanded = []
    for a in acts:
        if isinstance(a, dict) and "name" in a and "params" in a:
            props = spec.get(a["name"], {})
            expanded.append({a["name"]: {
                kv["key"]: _coerce(props.get(kv["key"], {"type": "string"}), kv["value"])
                for kv in a["params"] if isinstance(kv, dict) and "key" in kv
            }})
        else:
            expanded.append(a)  # the typed done branch
    return {**data, "action": expanded}


def make_structured_anthropic(model: str, api_key: str, base_url: str | None):
    """browser-use's ChatAnthropic, with the per-step output on structured outputs.

    Why: browser-use 0.13.10 asks for each step's AgentOutput through a forced
    tool call (tool_choice={"type":"tool"}). Claude Sonnet 5.5 / Opus 5.5 /
    Fable 5.1 reject forced tool_choice outright (HTTP 400 "tool_choice: type
    tool and any are not supported for this model" — measured 2026-10-02 on
    every step). Falling back to tool_choice=auto (the first fix) let the
    model flatten the schema ({"code": ...} with no `action` wrapper) or answer
    in prose — prod failed a trivial example.com task that way.

    The root fix is to stop smuggling JSON through a tool: send the AgentOutput
    schema as output_config.format (json_schema), which the API enforces
    with constrained decoding, so the response text IS a schema-valid AgentOutput.
    No tools, no tool_choice. Plain-text calls (output_format=None) are untouched.
    """
    from anthropic import APIConnectionError, APIStatusError, RateLimitError, omit
    from browser_use import ChatAnthropic
    from browser_use.llm.anthropic.serializer import AnthropicMessageSerializer
    from browser_use.llm.exceptions import ModelOutputTruncatedError, ModelProviderError, ModelRateLimitError
    from browser_use.llm.schema import SchemaOptimizer
    from browser_use.llm.views import ChatInvokeCompletion

    class StructuredOutputAnthropic(ChatAnthropic):
        async def ainvoke(self, messages, output_format=None, **kwargs):
            if output_format is None:
                return await super().ainvoke(messages, None, **kwargs)
            anthropic_messages, system_prompt = AnthropicMessageSerializer.serialize_messages(messages)
            schema, catalog, spec = compact_action_envelope(
                to_structured_output_schema(SchemaOptimizer.create_optimized_json_schema(output_format))
            )
            if catalog:
                if isinstance(system_prompt, list):
                    system_prompt = [*system_prompt, {"type": "text", "text": catalog}]
                else:
                    system_prompt = (system_prompt or "") + catalog
            params = self._get_client_params_for_invoke()
            extra_body = dict(params.pop("extra_body", None) or {})
            extra_body["output_config"] = {
                **(extra_body.get("output_config") or {}),
                "format": {"type": "json_schema", "schema": schema},
            }
            try:
                response = await self._create_message(
                    model=self.model,
                    messages=anthropic_messages,
                    system=system_prompt or omit,
                    extra_body=extra_body,
                    **params,
                )
            except APIConnectionError as e:
                raise ModelProviderError(message=e.message, model=self.name) from e
            except RateLimitError as e:
                raise ModelRateLimitError(message=e.message, model=self.name) from e
            except APIStatusError as e:
                raise ModelProviderError(message=e.message, status_code=e.status_code, model=self.name) from e
            if response.stop_reason == "refusal":
                # Checked BEFORE the text: a refused response carries a partial JSON
                # prefix, which otherwise surfaces as a misleading "Unterminated
                # string" parse error (it did, three steps out of five, until the
                # `thinking` output field was removed — see use_thinking in run_agent).
                details = self._get_stop_details(response) or {}
                raise ModelProviderError(
                    message=f"model refused this step (category={details.get('category')})", model=self.name
                )
            if response.stop_reason == "max_tokens":
                raise ModelOutputTruncatedError(
                    message=f"structured output truncated at max_tokens={self.max_tokens}", model=self.name
                )
            text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
            if not text:
                raise ModelProviderError(
                    message=f"no structured output (stop_reason={response.stop_reason})", model=self.name
                )
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    data = self._repair_serialized_fields(expand_action_envelope(data, spec))
            except ValueError as e:  # not JSON at all (cannot happen under constrained decoding)
                raise ModelProviderError(message=f"unparseable action params: {e}", model=self.name) from e
            return ChatInvokeCompletion(
                completion=output_format.model_validate(data),
                usage=self._get_usage(response),
                stop_reason=response.stop_reason,
                stop_details=self._get_stop_details(response),
            )

    # max_tokens 16000: the per-step JSON is small, but adaptive thinking (the
    # 5.5 default, which cannot be switched off with "disabled") shares the cap.
    # Server-side refusal fallback ("default" routes by refusal category; Claude
    # API only). A refused step is re-run on a fallback model inside the same
    # call instead of costing the agent a step. AGENT_LLM_FALLBACKS=off disables
    # it (e.g. behind a proxy, Bedrock or Vertex, which reject the parameter).
    fallback = os.environ.get("AGENT_LLM_FALLBACKS", "default") != "off" and not base_url
    return StructuredOutputAnthropic(
        model=model, api_key=api_key, base_url=base_url, max_retries=2, max_tokens=16000,
        output_config={"effort": os.environ.get("AGENT_LLM_EFFORT", "medium")},
        **({"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if fallback else {}),
    )


def make_llm(provider: str, model: str, api_key: str, base_url: str | None):
    p = provider.lower()
    if p == "anthropic":
        return make_structured_anthropic(model=model, api_key=api_key, base_url=base_url)
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


def forms_url(env=None) -> str:
    """The Camoufox that takes form submits: the dedicated forms service
    (CAMOUFOX_FORMS_URL) when set, else the shared one (CAMOUFOX_URL)."""
    env = os.environ if env is None else env
    return (env.get("CAMOUFOX_FORMS_URL") or "").strip() or env.get("CAMOUFOX_URL", "")


def build_tools(req: dict, state: dict, domains: list[str], deadline: float):
    """The action space and output model for one run (shared with the grammar test)."""
    from browser_use import Tools

    tools = Tools(exclude_actions=EXCLUDED_ACTIONS)
    if req.get("allow_form_submit"):
        build_form_bridge(tools, state, domains, deadline, forms_url())
    output_model = None
    if req.get("output_schema"):
        from browser_use.tools.extraction.schema_utils import schema_dict_to_pydantic_model
        output_model = schema_dict_to_pydantic_model(req["output_schema"])
    return tools, output_model


def agent_output_schema(tools, output_model=None) -> dict:
    """The exact json_schema a step request sends, for a given action space."""
    from browser_use.agent.views import AgentOutput
    from browser_use.llm.schema import SchemaOptimizer

    if output_model is not None:
        tools.use_structured_output_action(output_model)
    out = AgentOutput.type_with_custom_actions_no_thinking(tools.registry.create_action_model())  # = use_thinking=False
    return compact_action_envelope(to_structured_output_schema(SchemaOptimizer.create_optimized_json_schema(out)))[0]


def build_form_bridge(tools, state: dict, domains: list[str], deadline: float, camoufox_url: str):
    """Register submit_form_via_web_tools on a browser-use Tools instance."""
    from browser_use.agent.views import ActionResult
    from pydantic import BaseModel, Field

    # Deliberately lean: every field required, nothing nullable. Each optional or
    # nullable field grows the structured-output grammar, and this action is what
    # pushed the 15-action schema over the API's "compiled grammar is too large"
    # limit when it carried dismiss/success_url/captcha_field (measured
    # 2026-10-02). The single-POST guarantees live in /form-submit, not here.
    class BridgeField(BaseModel):
        selector: str = Field(..., description="CSS selector of the control")
        value: str = Field(..., description="text to type, option value for select, '' for check")
        action: Literal["type", "check", "select"]

    class SubmitFormParams(BaseModel):
        url: str = Field(..., description="URL of the page that holds the form")
        fields: list[BridgeField]
        submit: str = Field(..., description="CSS selector of the submit control")

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
            return ActionResult(error="form bridge unavailable: neither CAMOUFOX_FORMS_URL nor CAMOUFOX_URL is set on this sidecar")

        timeout_ms = int(min(240.0, remaining - 15) * 1000)
        body = {
            "url": params.url,
            "fields": [
                {"selector": f.selector, "action": f.action, **({"value": f.value} if f.action != "check" else {})}
                for f in params.fields
            ],
            "submit": params.submit,
            "timeout_ms": timeout_ms,
            "fresh_ip": True,
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
            tools, output_model = build_tools(req, state, domains, deadline)

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
                # No `thinking` field in the per-step output. Asking Claude 5.5 to
                # write its reasoning into the visible JSON trips the
                # reasoning_extraction safety classifier: stop_reason=refusal on
                # 3 of 5 steps of a trivial example.com task (measured 2026-10-02).
                # The model still thinks natively (adaptive thinking).
                use_thinking=False,
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
