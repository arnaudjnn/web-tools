"""Single-attempt forms with a browser lifetime independent of shared readers.

Each admitted operation gets a fresh thread and browser. A cancelled caller does
not free admission while its worker is still running; a worker wedged past its
deadline + teardown grace frees admission anyway and fails as unavailable. An
uncertain form is never replayed. Queue and launch time consume the same
deadline as page interactions.
"""
import asyncio
import faulthandler
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from form_flow import run_form, validate_form

log = logging.getLogger("camoufox.forms")

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


def not_started(url, error):
    return {"contract_version": 2, "status": 0, "url": url, "html": "",
            "ok": False, "form_submissions": 0, "error": error,
            "diagnostics": {"submit_click_attempted": False, "token_present": None}}


def run_isolated_form(browser_factory, *, deadline, **params):
    """No request can reach the target until browser and context are ready."""
    validate_form(params["url"], params.get("submission_urls"), params.get("success_url"))
    manager = context = None
    try:
        if time.monotonic() >= deadline:
            return not_started(params["url"], "deadline_before_browser")
        try:
            log.info("form phase: launching browser (headed=%s)", params.get("headed"))
            manager = browser_factory()
            browser = manager.__enter__()
            log.info("form phase: browser entered")
        except Exception as error:
            log.warning("form browser launch failed (%s: %s); no submission",
                        type(error).__name__, str(error)[:300])
            return not_started(params["url"], "browser_launch_failed")
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
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
        return run_form(context, timeout_ms=budget, **params)
    finally:
        for close in ([context.close] if context is not None else []) + (
                [lambda: manager.__exit__(None, None, None)] if manager is not None else []):
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
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                max(0.0, deadline - time.monotonic()) + TEARDOWN_GRACE_S)
        except asyncio.TimeoutError:
            if future.done():
                # The job itself finished with TimeoutError (form deadline) —
                # a normal outcome, not a wedge. Preserve its exception.
                return future.result()
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
            delay = _shed_delay_s()
            if delay > 0:
                # After the 502 has had time to flush: the wedged thread can
                # never run its finally, so its browser/context leak with it.
                log.warning("shedding wedged form worker in %.1fs (FORM_WEDGE_EXIT_S)"
                            " — leaked browser cannot be closed", delay)
                asyncio.get_running_loop().call_later(delay, os._exit, 1)
            raise TimeoutError("form outcome unavailable") from None
