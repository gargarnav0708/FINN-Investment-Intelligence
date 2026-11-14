"""Five-year fundamentals for any company from screener.in's standardised tables (built from filings).

Deterministic only, no model: trajectory (revenue / EBITDA / PAT, growth, margins), balance-sheet
quality (borrowings vs net worth) and cash quality (operating cash flow vs PAT). Every number is
labelled with its source and formula, and quarterly rows prefer machine-read results filings.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .guidance import delivered_table
from .models import Company, ProviderFinancialObservation

ANNUAL_KEYS = ("revenue", "ebitda", "pbt", "pat", "borrowings", "equity_capital", "reserves", "cfo", "cfi", "fixed_assets", "cwip", "investments", "total_assets")


def _fy(end: date) -> str:
    return f"FY{str(end.year)[-2:]}"


def _cagr(first: float | None, last: float | None, years: int) -> float | None:
    if not first or not last or first <= 0 or last <= 0 or years <= 0:
        return None
    return round(((last / first) ** (1 / years) - 1) * 100, 1)


def fundamentals(db: Session, company: Company) -> dict:
    rows = db.scalars(select(ProviderFinancialObservation).where(ProviderFinancialObservation.company_id == company.id,
                                                                 ProviderFinancialObservation.provider == "screener.in",
                                                                 ProviderFinancialObservation.period_type == "annual")).all()
    by_year: dict[date, dict] = defaultdict(dict)
    basis = None
    for row in rows:
        if row.canonical_metric in ANNUAL_KEYS:
            by_year[row.period_end][row.canonical_metric] = float(row.reported_value)
            if basis is None and "(" in (row.reported_label or ""):
                basis = row.reported_label.rsplit("(", 1)[-1].rstrip(")")
    # Balance sheets on screener.in can carry a half-year column (e.g. Sep): keep only the company's
    # fiscal year-end month, taken from the P&L years.
    from collections import Counter
    pl_months = Counter(end.month for end, v in by_year.items() if v.get("revenue") is not None)
    year_end_month = pl_months.most_common(1)[0][0] if pl_months else 3
    annual = []
    previous = None
    for end in sorted(e for e in by_year if e.month == year_end_month)[-6:]:
        v = by_year[end]
        net_worth = (v.get("equity_capital") or 0) + (v.get("reserves") or 0) if v.get("reserves") is not None else None
        item = {"period": _fy(end), "period_end": end.isoformat(), **{k: v.get(k) for k in ("revenue", "ebitda", "pat", "borrowings", "cfo", "cfi")},
                "net_worth": round(net_worth, 2) if net_worth else None}
        rev, ebitda, pat = v.get("revenue"), v.get("ebitda"), v.get("pat")
        item["ebitda_margin_pct"] = round(ebitda / rev * 100, 1) if rev and ebitda is not None else None
        item["pat_margin_pct"] = round(pat / rev * 100, 1) if rev and pat is not None else None
        item["revenue_growth_pct"] = round((rev / previous["revenue"] - 1) * 100, 1) if previous and previous.get("revenue") and rev else None
        item["debt_to_equity"] = round(v["borrowings"] / net_worth, 2) if v.get("borrowings") is not None and net_worth and net_worth > 0 else None
        item["cfo_to_pat"] = round(v["cfo"] / pat, 2) if v.get("cfo") is not None and pat and pat > 0 else None
        annual.append(item)
        previous = item
    annual = annual[-5:]

    signals = []
    if len(annual) >= 3:
        first, last = annual[0], annual[-1]
        years = len(annual) - 1
        rc, pc = _cagr(first.get("revenue"), last.get("revenue"), years), _cagr(first.get("pat"), last.get("pat"), years)
        if rc is not None:
            signals.append({"key": "revenue_cagr", "label": f"Revenue CAGR {first['period']}–{last['period']}", "value": rc, "unit": "%",
                            "formula": f"(revenue {last['period']} / revenue {first['period']})^(1/{years}) - 1"})
        if pc is not None:
            signals.append({"key": "pat_cagr", "label": f"PAT CAGR {first['period']}–{last['period']}", "value": pc, "unit": "%",
                            "formula": f"(PAT {last['period']} / PAT {first['period']})^(1/{years}) - 1"})
        if first.get("ebitda_margin_pct") is not None and last.get("ebitda_margin_pct") is not None:
            signals.append({"key": "margin_change", "label": f"EBITDA margin {first['period']} → {last['period']}",
                            "value": round(last["ebitda_margin_pct"] - first["ebitda_margin_pct"], 1), "unit": "pp",
                            "detail": f"{first['ebitda_margin_pct']}% → {last['ebitda_margin_pct']}%", "formula": "EBITDA / revenue; screener 'Operating Profit' (excludes other income)"})
        paired = [a for a in annual if a.get("cfo") is not None and a.get("pat") is not None]
        cfo_sum, pat_sum = sum(a["cfo"] for a in paired), sum(a["pat"] for a in paired)
        if pat_sum > 0 and cfo_sum and len(paired) >= 2:
            signals.append({"key": "cash_conversion", "label": f"Cash conversion, {paired[0]['period']}–{paired[-1]['period']}", "value": round(cfo_sum / pat_sum, 2), "unit": "×",
                            "formula": "Σ operating cash flow / Σ PAT", "flag": "watch" if cfo_sum / pat_sum < 0.8 else None})
        if first.get("borrowings") is not None and last.get("borrowings") is not None:
            signals.append({"key": "borrowings_change", "label": f"Borrowings {first['period']} → {last['period']}", "value": round(last["borrowings"] - first["borrowings"], 0),
                            "unit": "₹ cr", "detail": f"{first['borrowings']:,.0f} → {last['borrowings']:,.0f}", "formula": "screener 'Borrowings' (incl. lease liabilities where reported)",
                            "flag": "watch" if last.get("debt_to_equity") and last["debt_to_equity"] > 1 else None})
    flags = []
    if annual:
        last = annual[-1]
        if last.get("cfo_to_pat") is not None and last["cfo_to_pat"] < 0.7:
            flags.append(f"{last['period']} operating cash flow was only {last['cfo_to_pat']}× PAT: profits are not fully turning into cash.")
        if last.get("debt_to_equity") is not None and last["debt_to_equity"] > 1:
            flags.append(f"Borrowings are {last['debt_to_equity']}× net worth in {last['period']}.")
        margins = [a["ebitda_margin_pct"] for a in annual if a.get("ebitda_margin_pct") is not None]
        if len(margins) >= 3 and margins[-1] < margins[-2] < margins[-3]:
            flags.append("EBITDA margin has fallen for two consecutive years.")

    quarterly = delivered_table(db, company)["quarterly"][-8:]
    return {"ticker": company.ticker, "status": "available" if annual or quarterly else "unavailable",
            "source": "screener.in standardised statements (compiled from exchange filings); quarterly rows prefer FINN's machine-read results filings",
            "basis": basis or "consolidated", "unit": "INR crore", "annual": annual, "quarterly": quarterly, "signals": signals, "flags": flags,
            "message": None if annual or quarterly else "Run research for this company to load its five-year financials."}
