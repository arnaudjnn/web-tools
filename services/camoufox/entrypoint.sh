#!/bin/sh
# Container entrypoint: a display for opt-in headed browsers, then the app.
#
# WHY NOT xvfb-run. xvfb-run takes the X server args as one quoted string
# (-s "-screen ..."), and that nested quoting does not survive the trip from
# the Dockerfile CMD into the container's PID 1 (measured 2026-09-26: PID 1
# became `/bin/sh xvfb-run -a -s -screen 0 ...` with the quotes eaten, Xvfb
# started, uvicorn never did, and the service logged nothing). Starting Xvfb
# directly needs no quoting at all. Headless browsers ignore the display for
# the price of one Xvfb process; headed form browsers use DISPLAY :99.
: "${PORT:?PORT must be set — this image has no default, so a missing Railway variable fails here instead of silently handing callers a portless URL}"
export DISPLAY=:99
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
exec uvicorn app:app --host 0.0.0.0 --port "${PORT}" --workers "${WORKERS}"
