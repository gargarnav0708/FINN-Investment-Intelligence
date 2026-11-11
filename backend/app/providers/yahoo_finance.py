from __future__ import annotations

import time
from datetime import date, datetime, time as day_time, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

IST = ZoneInfo("Asia/Kolkata")


def is_session_complete(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    return now.timetz().replace(tzinfo=None) >= day_time(16, 0)


def fetch_history(symbol: str, months: int = 6, retries: int = 2, delay_seconds: float = 0.4, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(IST)
    today = now.date()
    start = today - timedelta(days=months * 31)
    # Yahoo can expose an unfinished daily candle. We only use dates before today.
    end = today + timedelta(days=1) if is_session_complete(now) else today
    last_error = None
    frame = None
    for attempt in range(retries + 1):
        try:
            frame = yf.Ticker(symbol).history(
                start=start.isoformat(), end=end.isoformat(), interval="1d",
                auto_adjust=False, actions=False, repair=False, timeout=20,
            )
            break
        except Exception as exc:  # provider failures are recorded per symbol by the caller
            last_error = exc
            if attempt < retries:
                time.sleep(delay_seconds * (attempt + 1))
    if frame is None:
        raise RuntimeError(f"Yahoo request failed for {symbol}: {last_error}") from last_error
    if frame.empty:
        return []
    frame = frame.sort_index()
    rows: list[dict] = []
    prior_close: float | None = None
    prior_volumes: list[int] = []
    for stamp, row in frame.iterrows():
        trading_day = stamp.date()
        if trading_day > today or (trading_day == today and not is_session_complete(now)):
            continue
        close = _number(row.get("Close"))
        volume = _integer(row.get("Volume"))
        avg20 = sum(prior_volumes[-20:]) / 20 if len(prior_volumes) >= 20 else None
        daily_return = ((close - prior_close) / prior_close * 100) if close is not None and prior_close and prior_close > 0 else None
        relative_volume = (volume / avg20) if volume is not None and avg20 and avg20 > 0 else None
        rows.append({
            "trading_date": trading_day,
            "open": _number(row.get("Open")),
            "high": _number(row.get("High")),
            "low": _number(row.get("Low")),
            "close": close,
            "adjusted_close": _number(row.get("Adj Close")),
            "volume": volume,
            "previous_close": prior_close,
            "daily_return_pct": daily_return,
            "average_volume_20": avg20,
            "relative_volume": relative_volume,
            "source": "Yahoo Finance via yfinance",
        })
        if close is not None:
            prior_close = close
        if volume is not None:
            prior_volumes.append(volume)
    return rows


def _number(value):
    try:
        if value is None or value != value:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value):
    try:
        n = _number(value)
        return int(n) if n is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
