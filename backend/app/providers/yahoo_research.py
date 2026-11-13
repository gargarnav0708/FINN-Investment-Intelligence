from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import yfinance as yf

PROVIDER = "Yahoo Finance via yfinance"
STATEMENTS = {
    "income": ("financials", "quarterly_financials"),
    "balance_sheet": ("balance_sheet", "quarterly_balance_sheet"),
    "cash_flow": ("cashflow", "quarterly_cashflow"),
}
LABELS = {
    "totalrevenue": "revenue", "revenue": "revenue", "ebitda": "ebitda",
    "operatingincome": "operating_profit", "pretaxincome": "pbt", "netincome": "pat",
    "totalassets": "total_assets", "totaldebt": "total_debt",
    "stockholdersequity": "shareholders_equity", "totalstockholderequity": "shareholders_equity",
    "cashandcashequivalents": "cash_and_cash_equivalents", "currentassets": "current_assets", "currentliabilities": "current_liabilities",
    "operatingcashflow": "operating_cash_flow", "capitalexpenditure": "capital_expenditure",
    "freecashflow": "free_cash_flow",
}


def exchange_symbol(exchange: str, ticker: str) -> str:
    exchange = exchange.upper()
    ticker = ticker.strip().upper()
    suffix = {"NSE": ".NS", "BSE": ".BO"}.get(exchange)
    if not suffix:
        raise ValueError("Exchange must be NSE or BSE.")
    return ticker if ticker.endswith(suffix) else f"{ticker}{suffix}"


def _quote_name(quote: dict) -> str:
    return str(quote.get("longname") or quote.get("shortname") or quote.get("displayName") or "").strip()


def search_listed_companies(query: str, limit: int = 8) -> list[dict]:
    """Search Yahoo's catalogue and retain only NSE/BSE equity listings."""
    query = query.strip()
    if len(query) < 2:
        return []
    result = yf.Search(query, max_results=max(limit * 2, 10), news_count=0)
    rows = []
    for item in result.quotes:
        symbol = str(item.get("symbol") or "").upper()
        quote_type = str(item.get("quoteType") or "").upper()
        exchange_code = str(item.get("exchange") or "").upper()
        if quote_type != "EQUITY" or not symbol.endswith((".NS", ".BO")):
            continue
        exchange = "NSE" if symbol.endswith(".NS") else "BSE"
        if exchange_code and exchange_code not in ({"NSI", "NSE"} if exchange == "NSE" else {"BSE", "BOM"}):
            continue
        rows.append({"name": _quote_name(item) or symbol.rsplit(".", 1)[0], "ticker": symbol.rsplit(".", 1)[0], "exchange": exchange, "yfinance_symbol": symbol})
        if len(rows) >= limit:
            break
    return rows


def company_profile(symbol: str) -> dict:
    ticker = yf.Ticker(symbol)
    try:
        info = ticker.get_info()
    except Exception:
        info = {}
    return {
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector") or info.get("industry"),
        "industry": info.get("industry"),
        "business": info.get("longBusinessSummary"),
        "website": info.get("website"),
        "currency": info.get("financialCurrency") or info.get("currency"),
        "exchange": info.get("exchange"),
        "provider": PROVIDER,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def _metric(label: str) -> str | None:
    key = re.sub(r"[^a-z0-9]", "", label.lower())
    return LABELS.get(key)


def _fiscal_period(end: date) -> tuple[str, str]:
    fy_end_year = end.year + (1 if end.month >= 4 else 0)
    fiscal_year = f"FY {fy_end_year - 1} - {fy_end_year}"
    fiscal_quarter = f"Q{((end.month - 4) % 12) // 3 + 1}"
    return fiscal_year, fiscal_quarter


def _number(value) -> Decimal | None:
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def fetch_financial_statements(symbol: str) -> tuple[dict, list[dict]]:
    """Fetch available income, balance-sheet and cash-flow periods without inventing gaps."""
    ticker = yf.Ticker(symbol)
    profile = company_profile(symbol)
    currency = profile.get("currency")
    observations = []
    for statement_type, (annual_name, quarterly_name) in STATEMENTS.items():
        for period_type, property_name in (("annual", annual_name), ("quarterly", quarterly_name)):
            try:
                frame = getattr(ticker, property_name)
            except Exception:
                continue
            if frame is None or getattr(frame, "empty", True):
                continue
            for column in frame.columns:
                period_end = column.date() if hasattr(column, "date") else column
                if not isinstance(period_end, date):
                    continue
                fy, fq = _fiscal_period(period_end)
                for reported_label, raw_value in frame[column].items():
                    label = str(reported_label)
                    canonical = _metric(label)
                    value = _number(raw_value)
                    if canonical is None or value is None:
                        continue
                    normalised = value / Decimal(10_000_000) if currency == "INR" else None
                    observations.append({
                        "statement_type": statement_type,
                        "period_type": period_type,
                        "period_end": period_end,
                        "fiscal_year": fy,
                        "fiscal_quarter": fq,
                        "canonical_metric": canonical,
                        "reported_label": label,
                        "reported_value": value,
                        "currency": currency,
                        "reported_unit": f"{currency} as reported by provider" if currency else "provider reported units",
                        "normalised_value_in_crore": normalised,
                        "provider": PROVIDER,
                        "source_url": f"https://finance.yahoo.com/quote/{symbol}/financials/",
                    })
    return profile, observations


def validate_symbol(symbol: str) -> dict:
    """Require a real, recent exchange listing before registering a symbol."""
    ticker = yf.Ticker(symbol)
    frame = ticker.history(period="5d", interval="1d", auto_adjust=False, actions=False, timeout=20)
    if frame is None or frame.empty:
        raise ValueError(f"Yahoo Finance returned no recent prices for {symbol}.")
    profile = company_profile(symbol)
    name = profile.get("name")
    if not name:
        # Search-result listings provide a checked issuer name for selection flows.
        rows = search_listed_companies(symbol.rsplit(".", 1)[0], limit=10)
        name = next((row["name"] for row in rows if row["yfinance_symbol"] == symbol), None)
    if not name:
        raise ValueError(f"Yahoo Finance could not verify the issuer identity for {symbol}.")
    return {**profile, "name": name, "symbol": symbol}
