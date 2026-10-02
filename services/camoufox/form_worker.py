"""Single-attempt forms with a browser lifetime independent of shared readers.

Each admitted operation gets a fresh thread and browser. A cancelled caller does
not free admission while its worker is still running; a worker wedged past its
deadline + teardown grace frees admission anyway and fails as unavailable. An
uncertain form is never replayed — except a FormRetryable: the flow parked on a
marked pre-POST driver call (bounded above its legitimate worst case), so no
POST left this machine and the caller may replay the same identity. Queue and
launch time consume the same deadline as page interactions.
"""
import asyncio
import faulthandler
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import form_flow
import launch_health
from form_flow import run_form, validate_form

log = logging.getLogger("camoufox.forms")


class FormRetryable(Exception):
    """Parked before the submit click: zero POSTs, identity untouched."""

# Beyond the operation's own deadline this is teardown budget: launch can start
# near the deadline and close/join must finish after it. Past that the job is
# wedged (a browser/driver join that never returns — observed 2026-09-27: a
# form thread held the admission gate forever while no browser existed, so
# every later form only ever saw queue_deadline_exceeded).
TEARDOWN_GRACE_S = 45

# Seconds after a wedge to hard-exit the process (0 = keep serving). A wedged
# worker holds its Camoufox manager inside a stuck greenlet: nobody can close
# it, so the firefox process leaks — threads and FDs accumulate until LAUNCHES
# start failing on the same replica (2026-09-28: 9 wedges alongside 65
# browser_launch_failed in three days, each redeploy resetting the count). A
# restart is the only reliable shed; callers already treat a dropped connection
# exactly like the 502 (outcome unknown, never resubmitted). Dockerfile sets 3;
# tests leave it unset so a wedge can never kill the suite.
def _shed_delay_s() -> float:
    try:
        return float(os.getenv("FORM_WEDGE_EXIT_S", "0"))
    except ValueError:
        return 0.0


def _schedule_shed(reason: str) -> None:
    """Hard-exit shortly after the response flushes (FORM_WEDGE_EXIT_S, 0 = never).

    The wedged thread's browser/context can never be closed — the handle is
    inside the stuck greenlet — so the leak is only ever shed with the process.
    """
    delay = _shed_delay_s()
    if delay > 0:
        log.warning("shedding form worker in %.1fs (FORM_WEDGE_EXIT_S) — %s", delay, reason)
        asyncio.get_running_loop().call_later(delay, os._exit, 1)


def not_started(url, error):
    return {"contract_version": 2, "status": 0, "url": url, "html": "",
            "ok": False, "form_submissions": 0, "error": error,
            "diagnostics": {"submit_click_attempted": False, "token_present": None}}


def run_isolated_form(browser_factory, *, deadline, runner=None, **params):
    """No request can reach the target until browser and context are ready.

    `runner` replaces run_form for a read-only job (form_inspect) that shares
    the same launch, retry and teardown."""
    validate_form(params["url"], params.get("submission_urls"), params.get("success_url"),
                  params.get("gate_text"), params.get("completion_markers"))
    manager = context = None
    context_owned = True
    try:
        # One launch retry: the first launch of a fresh process (tunnel still
        # coming up, proxy exit cycling) fails transiently, and today that
        # structured failure burns a whole caller attempt. Pre-navigation, so
        # zero submissions either way — the retry cannot double anything.
        for attempt in (1, 2):
            if time.monotonic() >= deadline:
                return not_started(params["url"], "deadline_before_browser")
            try:
                # `headed` lives in the browser_factory closure, never in params
                # (run_form would reject it as an unknown keyword) — log what is
                # knowable instead of a field that is always None here.
                log.info("form phase: launching browser (attempt %d)", attempt)
                manager = browser_factory()
                browser = manager.__enter__()
                launch_health.note(True)
                log.info("form phase: browser entered")
                break
            except Exception as error:
                launch_health.note(False)
                log.warning("form browser launch failed attempt %d (%s: %s); %s",
                            attempt, type(error).__name__, str(error)[:300],
                            "retrying" if attempt == 1 else "no submission")
                if manager is not None:
                    # A factory that opened before __enter__ raised still owns
                    # whatever it opened — close it here; the finally below
                    # only ever sees the manager the retry succeeded with.
                    try:
                        manager.__exit__(None, None, None)
                    except Exception:
                        pass
                manager = None
                if attempt == 2:
                    return not_started(params["url"], "browser_launch_failed")
                time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
        try:
            if hasattr(browser, "new_context"):
                context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
            else:
                # A persistent profile launches straight into ONE context —
                # there is no Browser to derive a fresh one from, and the
                # profile IS the identity (closing it here would burn the
                # warmth).
                context = browser
                context_owned = False
            log.info("form phase: context ready")
        except Exception as error:
            log.warning("form context failed (%s); no submission", type(error).__name__)
            return not_started(params["url"], "browser_context_failed")
        budget = int((deadline - time.monotonic()) * 1000)
        if budget <= 0:
            return not_started(params["url"], "deadline_before_navigation")
        # Do not catch unexpected exceptions here as "zero submissions": once
        # page execution starts, missing evidence means an unknown outcome.
        log.info("form phase: entering run_form (budget=%sms)", budget)
        return (runner or run_form)(context, timeout_ms=budget, **params)
    finally:
        closes = []
        if context is not None and context_owned:
            closes.append(context.close)
        if manager is not None:
            closes.append(lambda: manager.__exit__(None, None, None))
        for close in closes:
            try:
                close()
            except Exception as error:
                log.warning("form cleanup failed (%s)", type(error).__name__)


class FormWorker:
    def __init__(self):
        self._admission = asyncio.Lock()

    async def run(self, job, *, url, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return not_started(url, "queue_deadline_exceeded")
        try:
            await asyncio.wait_for(self._admission.acquire(), remaining)
        except asyncio.TimeoutError:
            return not_started(url, "queue_deadline_exceeded")
        executor = None
        state = {"released": False}

        def release_once():
            if not state["released"]:
                state["released"] = True
                self._admission.release()

        try:
            # A failed Playwright launch can leave its sync thread tainted.
            # Never reuse that thread, even if closing the manager failed.
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camoufox-form")
            # A previous job's marker must never age into this one's poll.
            form_flow.reset_live_step()
            future = asyncio.get_running_loop().run_in_executor(executor, job)
        except BaseException:
            if executor is not None:
                executor.shutdown(wait=False)
            release_once()
            raise

        def completed(task):
            executor.shutdown(wait=False)
            release_once()
            # Retrieve exceptions even when the HTTP caller disconnected.
            if not task.cancelled():
                task.exception()

        future.add_done_callback(completed)
        # Poll instead of one long wait: a pre-POST park (the transport behind
        # ANY marked call — input dispatch, locator resolution, evaluate — has
        # no timeout playwright enforces once the driver's loop is stuck) must
        # surface as FormRetryable seconds after it happens — long before
        # deadline + grace would fold it into the generic unknown outcome.
        hard_end = deadline + TEARDOWN_GRACE_S
        while True:
            left = hard_end - time.monotonic()
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), max(0.05, min(0.5, left)))
            except asyncio.TimeoutError:
                if future.done():
                    # The job itself finished with TimeoutError (form deadline)
                    # — a normal outcome, not a wedge. Preserve its exception.
                    return future.result()
                stuck = form_flow.pre_submit_hang_step()
                if stuck is not None:
                    # Parked before the submit click: no field touched, no POST
                    # left this machine, so the caller may replay the same
                    # identity once the shed has recycled the leak.
                    release_once()
                    log.warning("form job parked pre-submit at '%s' — zero POSTs,"
                                " retryable | future done=%s", stuck, future.done())
                    _schedule_shed("parked thread's browser cannot be closed")
                    raise FormRetryable(stuck) from None
                if time.monotonic() < hard_end:
                    continue
                # The job outlived deadline + teardown grace: wedged, not slow.
                # Give the gate back (the orphan may still finish later;
                # release_once keeps that from releasing twice) and fail as
                # UNAVAILABLE — no response means an unknown outcome, never
                # permission to resubmit.
                release_once()
                # Where the orphan's thread is BLOCKED is the only way to see an
                # unbounded call inside launch/teardown: nothing else logs before
                # the job returns, and it never does. Stacks are safe here — this
                # runs on the event loop, never inside the wedged thread itself.
                # faulthandler needs a real descriptor, hence the temp file. Thread
                # NAMES accompany the stacks: the form worker's absence or the
                # render worker's idleness reads identically without them.
                with tempfile.TemporaryFile("w+") as handle:
                    faulthandler.dump_traceback(handle, all_threads=True)
                    handle.seek(0)
                    stacks = handle.read()
                threads = [(t.name, hex(t.ident)) for t in threading.enumerate()]
                log.warning("form job wedged past deadline + grace; admission released, outcome unknown"
                            " | future done=%s threads=%s\n%s",
                            future.done(), threads, stacks.strip())
                _schedule_shed("leaked browser cannot be closed")
                raise TimeoutError("form outcome unavailable") from None
