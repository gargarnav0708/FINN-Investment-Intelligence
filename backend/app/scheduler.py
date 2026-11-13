"""End-of-day scheduler: on weekdays after the NSE close, refresh everything and evaluate alerts.

Runs as a daemon thread inside the web process (local runs, always-on hosts). On hosts that sleep
(Render free plan), the GitHub Actions workflow in .github/workflows/eod-refresh.yml calls
POST /api/refresh at the same time, which wakes the service and starts the identical job.

FINN_SCHEDULER=off disables it; FINN_EOD_TIME (HH:MM, IST, default 16:45) sets the run time.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, time as dtime
from typing import Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("finn.scheduler")
IST = ZoneInfo("Asia/Kolkata")
_started = False


def eod_time() -> dtime:
    raw = os.getenv("FINN_EOD_TIME", "16:45").strip()
    try:
        hours, minutes = (int(x) for x in raw.split(":"))
        return dtime(hours, minutes)
    except ValueError:
        return dtime(16, 45)


def due(now: datetime, last_run_date, run_at: dtime) -> bool:
    local = now.astimezone(IST)
    return local.weekday() < 5 and local.time() >= run_at and last_run_date != local.date()


def start_scheduler(last_run: Callable[[], datetime | None], launch: Callable[[], None], poll_seconds: int = 60) -> bool:
    global _started
    if _started or os.getenv("FINN_SCHEDULER", "on").lower() == "off" or "PYTEST_CURRENT_TEST" in os.environ:
        return False
    _started = True

    def loop() -> None:
        run_at = eod_time()
        log.info("FINN scheduler on: weekdays at %s IST", run_at.strftime("%H:%M"))
        while True:
            try:
                previous = last_run()
                previous_date = previous.astimezone(IST).date() if previous and previous.astimezone(IST).time() >= run_at else None
                if due(datetime.now(IST), previous_date, run_at):
                    log.info("FINN scheduler: starting end-of-day refresh")
                    launch()
            except Exception as exc:  # never let the thread die
                log.exception("scheduler tick failed: %s", exc)
            time.sleep(poll_seconds)

    threading.Thread(target=loop, name="finn-eod-scheduler", daemon=True).start()
    return True
