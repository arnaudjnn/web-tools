"""Single-attempt forms with a browser lifetime independent of shared readers.

Each admitted operation gets a fresh thread and browser. A cancelled caller does
not free admission while its worker is still running; an uncertain form is never
replayed. Queue and launch time consume the same deadline as page interactions.
"""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from form_flow import run_form, validate_form

log = logging.getLogger("camoufox.forms")


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
            manager = browser_factory()
            browser = manager.__enter__()
        except Exception as error:
            log.warning("form browser launch failed (%s); no submission", type(error).__name__)
            return not_started(params["url"], "browser_launch_failed")
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        except Exception as error:
            log.warning("form context failed (%s); no submission", type(error).__name__)
            return not_started(params["url"], "browser_context_failed")
        budget = int((deadline - time.monotonic()) * 1000)
        if budget <= 0:
            return not_started(params["url"], "deadline_before_navigation")
        # Do not catch unexpected exceptions here as "zero submissions": once
        # page execution starts, missing evidence means an unknown outcome.
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
        try:
            # A failed Playwright launch can leave its sync thread tainted.
            # Never reuse that thread, even if closing the manager failed.
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camoufox-form")
            future = asyncio.get_running_loop().run_in_executor(executor, job)
        except BaseException:
            if executor is not None:
                executor.shutdown(wait=False)
            self._admission.release()
            raise

        def completed(task):
            executor.shutdown(wait=False)
            self._admission.release()
            # Retrieve exceptions even when the HTTP caller disconnected.
            if not task.cancelled():
                task.exception()

        future.add_done_callback(completed)
        return await asyncio.shield(future)
