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

import launch_health
from form_flow import FormLive, run_form, validate_form

log = logging.getLogger("camoufox.forms")


class FormRetryable(Exception):
    """Parked before the submit click: zero POSTs, identity untouched."""

# Beyond the operation's own deadline this is teardown budget: launch can start
# near the deadline and close/join must finish after it. Past that the job is
# wedged (a browser/driver join that never returns — observed 2026-09-27: a
# form thread held the admission gate forever while no browser existed, so
# every later form only ever saw queue_deadline_exceeded).
TEARDOWN_GRACE_S = 45

# Launch attempts and the pause before each retry. Two were not enough on
# 2026-10-02 15:43:43/45: a persistent profile whose previous browser had
# just failed refused launch_persistent_context TWICE (TargetClosedError,
# 2s apart), and the next job's launch 6s later succeeded. A third attempt
# after 5s covers that window; still pre-navigation, so zero submissions.
LAUNCH_ATTEMPTS = 3
LAUNCH_BACKOFF_S = (2.0, 5.0)

# Exit rotation: a form whose reCAPTCHA library the exit truncates (or that
# stays unusable after its one reload) is relaunched ONCE on a fresh exit —
# still pre-input, zero POSTs — when the caller did not pin the exit. It
# needs a whole form's worth of budget left (launch + nav + fill + submit).
ROTATE_MIN_LEFT_S = 50.0


def _wants_rotation(result) -> bool:
    diagnostics = (result or {}).get("diagnostics") or {}
    return bool(result and result.get("error") == "captcha_unavailable"
                and result.get("form_submissions") == 0
                and not diagnostics.get("submit_click_attempted")
                and diagnostics.get("captcha_rotate"))

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


def run_isolated_form(browser_factory, *, deadline, runner=None, live=None,
                      rotate_factory=None, **params):
    """No request can reach the target until browser and context are ready.

    `runner` replaces run_form for a read-only job (form_inspect) that shares
    the same launch, retry and teardown.

    `rotate_factory` (form jobs only; None = the exit is pinned) returns a
    browser factory on a FRESH exit: a run that ends captcha_unavailable with
    `captcha_rotate` (zero POSTs, nothing touched) is torn down and run ONCE
    more on it.

    Emits the job's one `form-run` summary line on every way out (a
    structured failure, a runner result, or an exception escaping), after
    teardown so its duration is in the line.
    """
    live = live if live is not None else FormLive()
    if runner is not None:
        live.note(runner=getattr(runner, "__name__", "runner"))
    result = failure = None
    try:
        result = _run_rotating(browser_factory, deadline, live, runner, params, rotate_factory)
        return result
    except BaseException as error:
        failure = type(error).__name__
        raise
    finally:
        if result is not None:
            live.emit(result)
        else:
            live.emit(None, error="exception", failure_class=failure)


def _run_rotating(browser_factory, deadline, live, runner, params, rotate_factory):
    if runner is not None or rotate_factory is None:
        return _run_isolated(browser_factory, deadline, live, runner, params)
    result = _run_isolated(browser_factory, deadline, live, runner,
                           dict(params, exit_rotatable=True))
    if not _wants_rotation(result) or deadline - time.monotonic() < ROTATE_MIN_LEFT_S:
        return result
    first = (result.get("diagnostics") or {}).get("captcha_failed") or []
    live.note(exit_rotated=True,
              first_captcha_failed=[{k: f.get(k) for k in ("path", "code", "after_response")}
                                    for f in first][:5])
    log.info("form phase: reCAPTCHA unusable on this exit; relaunching once on a fresh exit")
    result = _run_isolated(rotate_factory(), deadline, live, runner,
                           dict(params, exit_rotatable=False))
    result.setdefault("diagnostics", {})["exit_rotated"] = True
    return result


def _run_isolated(browser_factory, deadline, live, runner, params):
    validate_form(params["url"], params.get("submission_urls"), params.get("success_url"),
                  params.get("gate_text"), params.get("completion_markers"))
    manager = context = None
    context_owned = True
    live.enter("launch")
    try:
        # Launch retries: the first launch of a fresh process (tunnel still
        # coming up, proxy exit cycling) fails transiently, and so does a
        # profile whose previous browser is still letting go of it (see
        # LAUNCH_ATTEMPTS). Pre-navigation, so zero submissions either way —
        # a retry cannot double anything.
        for attempt in range(1, LAUNCH_ATTEMPTS + 1):
            if time.monotonic() >= deadline:
                return not_started(params["url"], "deadline_before_browser")
            live.note(launch_attempts=attempt)
            try:
                # `headed` lives in the browser_factory closure, never in params
                # (run_form would reject it as an unknown keyword) — log what is
                # knowable instead of a field that is always None here.
                log.info("form phase: launching browser (attempt %d)", attempt)
                manager = browser_factory()
                browser = manager.__enter__()
                launch_health.note(True)
                version = getattr(browser, "version", None)
                if isinstance(version, str) and version:
                    live.note(browser_version=version)
                log.info("form phase: browser entered")
                break
            except Exception as error:
                launch_health.note(False)
                live.note(launch_errors=[*live.extra.get("launch_errors", []),
                                         type(error).__name__])
                log.warning("form browser launch failed attempt %d (%s: %s); %s",
                            attempt, type(error).__name__, str(error)[:300],
                            "retrying" if attempt < LAUNCH_ATTEMPTS else "no submission")
                if manager is not None:
                    # A factory that opened before __enter__ raised still owns
                    # whatever it opened — close it here; the finally below
                    # only ever sees the manager the retry succeeded with.
                    try:
                        manager.__exit__(None, None, None)
                    except Exception:
                        pass
                manager = None
                if attempt == LAUNCH_ATTEMPTS:
                    return not_started(params["url"], "browser_launch_failed")
                pause = LAUNCH_BACKOFF_S[min(attempt - 1, len(LAUNCH_BACKOFF_S) - 1)]
                time.sleep(min(pause, max(0.0, deadline - time.monotonic())))
        try:
            if hasattr(browser, "new_context"):
                # The launch's own context options (fingerprint.viewport);
                # a factory that names none keeps the fixed 1440x900 viewport.
                options = getattr(manager, "form_context_options", None)
                if not isinstance(options, dict):
                    options = {"viewport": {"width": 1440, "height": 900}}
                live.note(viewport="fixed" if "viewport" in options else "native")
                context = browser.new_context(**options, service_workers="block")
            else:
                # A persistent profile launches straight into ONE context —
                # there is no Browser to derive a fresh one from, and the
                # profile IS the identity (closing it here would burn the
                # warmth).
                context = browser
                context_owned = False
                live.note(viewport="profile")
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
        if runner is not None:
            # A read-only runner (no input, so no pre-POST marks to poll).
            live.enter("runner")
            return runner(context, timeout_ms=budget, **params)
        return run_form(context, timeout_ms=budget, live=live, **params)
    finally:
        live.enter("teardown")
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
    def __init__(self, concurrency=None):
        # FORM_CONCURRENCY forms run at once per process (default 1, the old
        # serial admission). Each job still gets its own thread and browser;
        # a wedge shed (os._exit) takes every in-flight job with it, which
        # callers already read as an unknown outcome.
        if concurrency is None:
            try:
                concurrency = int(os.getenv("FORM_CONCURRENCY", "1"))
            except ValueError:
                concurrency = 1
        self.concurrency = max(1, concurrency)
        # Created on first use, inside the running loop (a primitive made
        # outside it binds to the wrong loop on older Pythons).
        self._sem = None

    @property
    def _admission(self):
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
        return self._sem

    async def run(self, job, *, url, deadline, live=None):
        """Run `job` on a fresh thread; `live` is the job's FormLive.

        The poll reads ITS mark (never a module global), and every way out
        that the job itself cannot report — never admitted, parked, wedged —
        emits the job's summary line from here. Once per job: the orphan of
        a park finishing later cannot log a second verdict.
        """
        live = live if live is not None else FormLive()
        live.enter("queue")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result = not_started(url, "queue_deadline_exceeded")
            live.emit(result)
            return result
        try:
            await asyncio.wait_for(self._admission.acquire(), remaining)
        except asyncio.TimeoutError:
            result = not_started(url, "queue_deadline_exceeded")
            live.emit(result)
            return result
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
                stuck = live.hang_step()
                if stuck is not None:
                    # Parked before the submit click: no field touched, no POST
                    # left this machine, so the caller may replay the same
                    # identity once the shed has recycled the leak.
                    release_once()
                    log.warning("form job parked pre-submit at '%s' — zero POSTs,"
                                " retryable | future done=%s", stuck, future.done())
                    live.emit(None, error="parked", parked_step=stuck)
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
                live.emit(None, error="wedged", parked_step=live.step or None)
                _schedule_shed("leaked browser cannot be closed")
                raise TimeoutError("form outcome unavailable") from None
