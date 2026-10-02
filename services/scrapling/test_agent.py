"""
web_agent tests: a scripted (mocked) LLM drives a real Patchright Chromium
against a local static server. No network, no LLM key, no cost.

Run with the AGENT venv's interpreter (it has browser-use + patchright; add
fastapi+httpx to it for the endpoint tests):

    /opt/agent-venv/bin/python -m unittest services/scrapling/test_agent.py

What is proven here:
  - the loop: navigate → act → done returns a structured final_result;
  - the step cap: a model that never finishes stops at max_steps;
  - mutation blocking: a form.submit() and a same-domain fetch POST from the
    agent's own browser never reach the server; allow_mutations lets them through;
  - the domain fence: a navigation off allowed_domains is aborted;
  - the endpoint: 503 {disabled} without a key, 400 without a fence, and a full
    subprocess round-trip through agent.py.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_runner as ar  # noqa: E402

HAVE_BROWSER = all(importlib.util.find_spec(m) for m in ("browser_use", "patchright"))
HAVE_FASTAPI = all(importlib.util.find_spec(m) for m in ("fastapi", "httpx"))

INDEX = """<!doctype html><html><head><title>Acme home</title></head><body>
<h1>Acme</h1><a href="/pricing.html">Pricing</a>
<form id="f" method="post" action="/submit"><input name="email" value="a@b.c"><button type="submit">Send</button></form>
</body></html>"""
PRICING = """<!doctype html><html><head><title>Acme pricing</title></head><body>
<h1>Plans</h1><ul><li>Starter</li><li>Pro</li><li>Enterprise</li></ul></body></html>"""


class _Handler(SimpleHTTPRequestHandler):
    posts: list[str] = []
    get_hosts: list[str] = []
    bridge_calls: list[dict] = []

    def do_GET(self):  # noqa: N802
        _Handler.get_hosts.append((self.headers.get("Host") or "").split(":")[0])
        body = {"/": INDEX, "/index.html": INDEX, "/pricing.html": PRICING}.get(self.path.split("?")[0])
        if body is None:
            self.send_error(404)
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if self.path == "/form-submit":  # stands in for Camoufox
            _Handler.bridge_calls.append(json.loads(raw))
            data = json.dumps({
                "contract_version": 2, "form_submissions": 1, "error": None, "status": 200,
                "url": "https://forms.test/thanks", "html": "<p>Thank you</p>", "ok": True,
                "exit_session": "", "diagnostics": {},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        _Handler.posts.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>thanks</body></html>")

    def log_message(self, *a):
        pass


def step(*actions: dict, goal: str = "next") -> dict:
    return {"evaluation_previous_goal": "ok", "memory": "", "next_goal": goal, "action": list(actions)}


class PureHelpers(unittest.TestCase):
    def test_normalize_domains(self):
        self.assertEqual(ar.normalize_domains(None, "https://www.example.com/x"), ["example.com"])
        self.assertEqual(ar.normalize_domains(["*.Foo.com", "https://bar.io/p", "foo.com"], None), ["foo.com", "bar.io"])

    def test_host_allowed(self):
        self.assertTrue(ar.host_allowed("https://a.example.com/", ["example.com"]))
        self.assertTrue(ar.host_allowed("about:blank", ["example.com"]))
        self.assertFalse(ar.host_allowed("https://notexample.com/", ["example.com"]))

    def test_mutation_rule(self):
        d = ["example.com"]
        self.assertTrue(ar.is_blocked_mutation("POST", "document", "https://other.net/x", d))
        self.assertTrue(ar.is_blocked_mutation("PUT", "fetch", "https://api.example.com/x", d))
        self.assertFalse(ar.is_blocked_mutation("POST", "xhr", "https://analytics.net/c", d))
        self.assertFalse(ar.is_blocked_mutation("GET", "document", "https://example.com/", d))

    def test_step_records_carry_no_secrets(self):
        s = ar.describe_action({"input": {"index": 4, "text": "hunter2"}})
        self.assertEqual(s, "input(index=4)")
        s = ar.describe_action({"navigate": {"url": "https://x.com/p?token=abc", "new_tab": False}})
        self.assertNotIn("token", s)


@unittest.skipUnless(HAVE_BROWSER, "browser-use/patchright not installed in this interpreter")
class AgentLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["AGENT_PROFILE_DIR"] = cls.tmp.name

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        _Handler.posts.clear()
        _Handler.get_hosts.clear()
        _Handler.bridge_calls.clear()

    def run_agent(self, script: list[dict], **req) -> dict:
        full = {"task": "test", "start_url": self.base + "/", "max_steps": 10, "timeout_ms": 90_000, **req}
        return asyncio.run(ar.run_agent(full, llm=ar.ScriptedLLM(script)))

    def test_loop_returns_structured_result(self):
        r = self.run_agent(
            [
                step({"navigate": {"url": self.base + "/pricing.html"}}),
                step({"done": {"success": True, "data": {"plans": ["Starter", "Pro", "Enterprise"]}}}),
            ],
            output_schema={"type": "object", "properties": {"plans": {"type": "array", "items": {"type": "string"}}}},
        )
        self.assertTrue(r["success"], r)
        self.assertEqual(r["final_result"], {"plans": ["Starter", "Pro", "Enterprise"]})
        self.assertIsNone(r["error"])
        self.assertTrue(any(u.endswith("/pricing.html") for u in r["urls"]), r["urls"])
        self.assertTrue(any(s["action"].startswith("navigate(") for s in r["steps"]), r["steps"])
        for s in r["steps"]:
            self.assertEqual(set(s) - {"error"}, {"n", "action", "url"})

    def test_step_cap(self):
        r = self.run_agent([step({"navigate": {"url": self.base + "/pricing.html"}})], max_steps=3)
        self.assertFalse(r["success"])
        self.assertEqual(r["steps"][0]["n"], 0, r["steps"])  # the start_url navigation
        self.assertEqual([s["n"] for s in r["steps"] if s["n"] >= 1], [1, 2, 3], r["steps"])
        # The last step was forced to `done` with success=false by the cap.
        self.assertEqual(r["final_result"], "step cap reached")

    def test_wall_clock_deadline_returns_partial_result(self):
        r = self.run_agent([step({"wait": {"seconds": 4}})], max_steps=40, timeout_ms=10_000)
        self.assertFalse(r["success"])
        self.assertLess(r["duration_s"], 16, r)
        self.assertLess(len(r["steps"]), 10)

    def _mutating_script(self):
        return [
            step({"evaluate": {"code": "(async()=>{try{await fetch('/api/save',{method:'POST',body:'x'});return 'sent'}catch(e){return 'blocked'}})()"}}),
            step({"evaluate": {"code": "document.getElementById('f').submit(); 'submitted'"}}),
            step({"wait": {"seconds": 1}}),
            step({"done": {"success": True, "text": "tried"}}),
        ]

    def test_mutations_blocked_by_default(self):
        r = self.run_agent(self._mutating_script())
        self.assertEqual(_Handler.posts, [], "a mutating request reached the server")
        kinds = {(b["method"], b["type"]) for b in r["blocked_requests"] if b["reason"] == "mutation"}
        self.assertIn(("POST", "fetch"), kinds, r["blocked_requests"])
        self.assertIn(("POST", "document"), kinds, r["blocked_requests"])

    def test_allow_mutations_lets_them_through(self):
        r = self.run_agent(self._mutating_script(), allow_mutations=True)
        self.assertIn("/api/save", _Handler.posts)
        self.assertIn("/submit", _Handler.posts)
        self.assertEqual([b for b in r["blocked_requests"] if b["reason"] == "mutation"], [])

    def test_form_bridge_submits_once_through_camoufox(self):
        os.environ["CAMOUFOX_URL"] = self.base  # the fake /form-submit above
        call = {"submit_form_via_web_tools": {
            "url": self.base + "/", "submit": "#f button",
            "fields": [{"selector": "input[name=email]", "value": "secret@example.com"}],
        }}
        try:
            r = self.run_agent(
                [step(call), step(call), step({"done": {"success": True, "text": "sent"}})],
                allow_form_submit=True,
            )
        finally:
            os.environ.pop("CAMOUFOX_URL", None)
        self.assertEqual(len(_Handler.bridge_calls), 1, "the bridge must POST exactly once per run")
        self.assertEqual(_Handler.bridge_calls[0]["fields"][0]["value"], "secret@example.com")
        self.assertEqual(_Handler.posts, [], "the agent's own browser must not have posted")
        self.assertEqual(len(r["form_submissions"]), 1)
        self.assertEqual(r["form_submissions"][0]["outcome"], "submitted")
        self.assertNotIn("secret@example.com", json.dumps(r), "typed values must not echo back")
        self.assertTrue(any("single-POST" in (s.get("error") or "") for s in r["steps"]), r["steps"])

    def test_form_bridge_absent_unless_opted_in(self):
        call = {"submit_form_via_web_tools": {"url": self.base + "/", "submit": "#f button", "fields": []}}
        r = self.run_agent([step(call), step({"done": {"success": True, "text": "x"}})])
        self.assertEqual(_Handler.bridge_calls, [])
        self.assertEqual(r["form_submissions"], [])

    def test_domain_fence(self):
        off = f"http://localhost:{self.port}/pricing.html"
        r = self.run_agent([step({"navigate": {"url": off}}), step({"done": {"success": False, "text": "x"}})])
        # Either browser-use refused it up front or the network guard aborted it;
        # the off-fence host must never have been asked for anything.
        self.assertNotIn("localhost", _Handler.get_hosts, r)
        self.assertIn("127.0.0.1", _Handler.get_hosts)


@unittest.skipUnless(HAVE_FASTAPI and HAVE_BROWSER, "fastapi/httpx + browser-use needed for endpoint tests")
class Endpoint(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import agent

        app = FastAPI()
        app.include_router(agent.router)
        self.client = TestClient(app)
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        os.environ["AGENT_PYTHON"] = sys.executable
        os.environ["AGENT_LOCK_PATH"] = os.path.join(self.tmp.name, "lock")
        os.environ["AGENT_PROFILE_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()

    def test_disabled_without_key(self):
        os.environ.pop("AGENT_LLM_API_KEY", None)
        os.environ["AGENT_LLM_PROVIDER"] = "anthropic"
        r = self.client.post("/agent", json={"task": "x", "start_url": "https://example.com"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"disabled": True, "reason": "set AGENT_LLM_API_KEY"})

    def test_requires_a_fence(self):
        os.environ["AGENT_LLM_API_KEY"] = "k"
        r = self.client.post("/agent", json={"task": "x"})
        self.assertEqual(r.status_code, 400)

    def test_max_steps_bounded(self):
        os.environ["AGENT_LLM_API_KEY"] = "k"
        r = self.client.post("/agent", json={"task": "x", "start_url": "https://e.com", "max_steps": 41})
        self.assertEqual(r.status_code, 422)

    def test_subprocess_round_trip(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        script = Path(self.tmp.name) / "script.json"
        script.write_text(json.dumps([step({"done": {"success": True, "text": "home seen"}})]))
        os.environ["AGENT_LLM_PROVIDER"] = "scripted"
        os.environ["AGENT_LLM_API_KEY"] = str(script)
        try:
            r = self.client.post("/agent", json={"task": "look", "start_url": base + "/", "timeout_ms": 60_000})
        finally:
            server.shutdown()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["success"], body)
        self.assertEqual(body["final_result"], "home seen")
        self.assertEqual(body["allowed_domains"], ["127.0.0.1"])


if __name__ == "__main__":
    unittest.main()
