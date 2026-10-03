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
#
# WHY THE WATCHDOG + STALE-FILE CLEANUP. A Railway restart reuses the
# container's writable layer: the dead X server's /tmp/.X99-lock and
# /tmp/.X11-unix/X99 survive it, the next Xvfb exits with "Server is already
# active for display 99", and every HEADED launch dies from then on — while
# headless renders keep answering, hiding the breakage (measured 2026-09-29:
# form launches failed 6/6 for hours across restarts; a redeploy, which
# recreates the filesystem, was the only cure). So: probe the display, and
# when nothing answers clear the stale files and start Xvfb again — at boot
# and every few seconds after, which also covers an Xvfb killed mid-life
# (OOM) without any restart at all. The probe connects and hangs up without
# sending bytes, which a waiting X server treats as any other client that
# changed its mind.
: "${PORT:?PORT must be set — this image has no default, so a missing Railway variable fails here instead of silently handing callers a portless URL}"
export DISPLAY=:99
# The display's geometry bounds the screen Camoufox draws for headed form
# browsers (camoufox clamps the fingerprint's screen to the largest display).
# Override only to measure a larger-screen fingerprint; 1440x900 is production.
: "${XVFB_SCREEN:=1440x900x24}"
(
  while :; do
    if ! python3 -c "import socket; s = socket.socket(socket.AF_UNIX); s.connect('/tmp/.X11-unix/X99')" 2>/dev/null; then
      rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
      Xvfb :99 -screen 0 "${XVFB_SCREEN}" -nolisten tcp &
    fi
    sleep 5
  done
) &
exec uvicorn app:app --host 0.0.0.0 --port "${PORT}" --workers "${WORKERS}"
