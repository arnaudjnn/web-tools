---
name: tools-health
description: Check whether the web-tools Railway stack is actually working and heal it when it is not. Diagnoses from three sources (service state, known log signatures, and live tool probes that assert which BACKEND served a request), then applies only the remediations known to fix this stack (evict a dead browser pool, recycle a wedged anti-bot session, redeploy a degraded service). Use whenever web_search returns nothing, a fetch is slow or comes back from the wrong engine, a deploy looks green but behaves wrong, or the user asks to check Railway logs, check tool health, or fix/heal the tools.
---

# tools-health

Diagnose and repair the web-tools stack: `Tools`, `SearXNG`,
`Scrapling`, `Camoufox`, `Redis` in project `3375ebc9-cb5f-42ac-999d-b1d3b8feb5ef`.

## Why liveness checks are not enough here

There are two fetchers behind a symmetric fallback, and when the right one is
unavailable a request silently falls back to the other. The caller gets a real
page and a 200 with quietly wrong provenance: LinkedIn fetched from the Italian
exit, or an Italian source fetched from a US one. `web_search` fails the same
way, returning `[]` with a 200.

So this skill asserts on **which backend served the request** and on **result
counts**, never on liveness alone. Read `references/signatures.md` before
interpreting anything; it maps each signature to its confirmed cause and fix.

## Workflow

1. **Confirm the project is linked.** `railway status` should print
   `Project: web-tools`. If not, ask the user to run `! railway link` (it is
   interactive).

2. **Diagnose.** Read-only, ~60s:
   ```bash
   python3 .claude/skills/tools-health/scripts/health.py
   ```
   `--fast` skips the log scan and deep probes (~10s, for a SessionStart hook).
   `--json` emits machine-readable findings for step 3.
   Exit code is non-zero when there is an error-severity finding.

3. **Heal, deliberately.** Look at the plan before acting:
   ```bash
   python3 .claude/skills/tools-health/scripts/health.py --json > /tmp/h.json
   python3 .claude/skills/tools-health/scripts/heal.py --findings /tmp/h.json
   python3 .claude/skills/tools-health/scripts/heal.py --findings /tmp/h.json --apply
   ```
   Redeploys are rate-limited to one per service per 15 minutes, so a crashloop
   cannot be amplified into a restart loop.

4. **Verify, do not assume.** Re-run step 2. A redeploy that "succeeded" is not
   evidence: this stack's failures survive green deploys. The probe asserting
   `mode=stealth` is the evidence.

5. **If a finding is `manual`**, stop and report it. Those are the ones where
   guessing makes things worse: a lockfile mismatch, a service building the wrong
   Dockerfile, a variable reference resolving to empty.

## What it will not do

`heal.py` cannot delete a service, change a domain, edit a variable, or scale
anything. Those either cannot be undone by retrying, or change the security
posture. Only `Tools` should have a public domain, and removing a domain is what
makes a service private. If the fix is one of those, it is reported as `manual`.

## Running it on a schedule

Three different triggers, for three different needs. Pick by how long you need the
watching to outlive the conversation.

- **Now, once.** Invoke this skill. Best when you are already debugging.

- **Every session in this project.** A `SessionStart` hook in
  `.claude/settings.json` running `health.py --fast`. Its output becomes context,
  so a session opens already knowing whether the stack is degraded. Keep it
  `--fast` and read-only: it runs on *every* session start, and a 60s sweep there
  would be a tax on every conversation. A hook cannot heal, being a shell command
  rather than a Claude turn; it only surfaces the problem for you to act on.
  Use the `update-config` skill to add it.

- **While you are away.** `/schedule` a routine, which runs as a cloud agent on a
  cron with no session attached. This is the only option that keeps healing
  overnight.

  `/loop 15m` also works but only for the life of the current session, so it suits
  babysitting a specific deploy rather than standing guard.

**Loop hygiene**, which matters because every iteration costs tokens:

- Prefer being notified over polling. Do not loop to wait for something that
  reports back on its own.
- Match the interval to how fast the thing you are watching actually changes.
  Minutes, not seconds, for a Railway service.
- Detection loops are cheap to get right; healing loops need bounded, idempotent
  actions and a cooldown, or a flapping service gets restarted forever. That is
  why the cooldown lives in `heal.py` rather than in the loop prompt.
- Report "nothing changed" cheaply so a quiet hold stays quiet.
