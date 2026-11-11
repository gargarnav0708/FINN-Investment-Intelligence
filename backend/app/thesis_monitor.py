"""Deterministic, evidence-linked thesis drift signals from validated filings."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Announcement, Company, FinancialObservation
from .intelligence import classify_announcement

RULE_VERSION = "thesis-drift-v1"
MATERIAL_CHANGE_PCT = 1.0
MATERIAL_CHANGE_CRORE = 1.0


def _obs_payload(row: FinancialObservation) -> dict:
    return {
        "id": row.id, "metric": row.canonical_metric,
        "value_crore": float(row.normalised_value_in_crore),
        "period_type": row.period_type, "fiscal_year": row.fiscal_year,
        "fiscal_quarter": row.fiscal_quarter, "period_end": row.period_end.isoformat(),
        "statement_basis": row.statement_basis, "document_id": row.document_id,
        "document_title": row.document.title, "page_number": row.page_number,
        "row_label": row.row_label, "column_header": row.column_header,
        "source_quote": row.source_quote,
        "url": f"/api/documents/{row.document_id}/file#page={row.page_number}",
    }


def _rows(db: Session, company: Company, metric: str, basis: str, period_type: str) -> list[FinancialObservation]:
    rows = db.scalars(select(FinancialObservation).where(
        FinancialObservation.company_id == company.id,
        FinancialObservation.canonical_metric == metric,
        FinancialObservation.statement_basis == basis,
        FinancialObservation.period_type == period_type,
        FinancialObservation.extraction_status == "validated",
    ).order_by(FinancialObservation.period_end, FinancialObservation.id)).all()
    # Prefer the observation registered against the filing for that same period.
    selected: dict[date, FinancialObservation] = {}
    for row in rows:
        current = selected.get(row.period_end)
        is_primary = row.fiscal_year == row.document.fiscal_year and (
            row.period_type == "annual" and row.document.document_type == "annual_report"
            or row.period_type == "quarterly" and row.fiscal_quarter == row.document.quarter
        )
        current_primary = bool(current and current.fiscal_year == current.document.fiscal_year and (
            current.period_type == "annual" and current.document.document_type == "annual_report"
            or current.period_type == "quarterly" and current.fiscal_quarter == current.document.quarter
        ))
        if current is None or is_primary or not current_primary:
            selected[row.period_end] = row
    return [selected[key] for key in sorted(selected)]


def _change_status(current: float | None, previous: float | None, *, higher_is_better: bool = True) -> str:
    if current is None or previous is None:
        return "insufficient_data"
    if abs(previous) < 1e-9:
        return "insufficient_data"
    change = (current / previous - 1) * 100
    if abs(change) < MATERIAL_CHANGE_PCT:
        return "no_material_change"
    favorable = change > 0 if higher_is_better else change < 0
    return "improving" if favorable else "deteriorating"


def _signal(code: str, name: str, status: str, explanation: str, formula: str | None,
            current: float | None = None, previous: float | None = None,
            current_period: str | None = None, previous_period: str | None = None,
            evidence: list[FinancialObservation] | None = None, unit: str = "INR crore",
            next_step: str | None = None) -> dict:
    suggested_next_steps = {
        "revenue_pat_growth": "Inspect operating costs, interest expense and other reported items in the cited results.",
        "pbt_margin": "Compare the income-statement rows and reporting basis in the cited filing.",
        "pat_margin": "Compare the income-statement rows and reporting basis in the cited filing.",
        "cash_conversion": "Compare the cash-flow statement with PAT for the same reporting period.",
        "free_cash_flow": "Review operating cash generation alongside the separately reported capex line.",
        "net_debt": "Review borrowings, cash balances and the filing's capital-allocation disclosures.",
        "debt_equity": "Review the borrowing and equity notes, with lease liabilities kept separate.",
        "current_ratio": "Inspect current-asset and current-liability notes in the cited statement.",
    }
    return {
        "code": code, "name": name, "status": status, "current_value": current,
        "previous_value": previous, "unit": unit, "current_period": current_period,
        "previous_period": previous_period, "formula": formula, "explanation": explanation,
        "supporting_observations": [_obs_payload(row) for row in (evidence or [])],
        "human_review_required": status == "watch", "rule_version": RULE_VERSION,
        "next_step": next_step or suggested_next_steps.get(code),
    }


def build_thesis_drift(db: Session, company: Company, basis: str = "consolidated",
                       period_type: str = "annual", periods: int = 3) -> dict:
    metric_names = ("revenue", "pat", "pbt", "ebitda", "cash_from_operations", "capital_expenditure_ppe",
                    "borrowings_noncurrent", "borrowings_current", "cash_and_cash_equivalents",
                    "total_equity", "current_assets", "current_liabilities")
    series = {metric: _rows(db, company, metric, basis, period_type)[-periods:] for metric in metric_names}
    periods_found = sorted({row.period_end for rows in series.values() for row in rows})
    labels = {row.period_end: (row.fiscal_year if period_type == "annual" else f"{row.fiscal_quarter} {row.fiscal_year}")
              for rows in series.values() for row in rows}
    period_label = lambda value: labels.get(value, value.isoformat()) if value else None
    signal_groups: list[dict] = []

    def get(metric: str, period: date | None) -> FinancialObservation | None:
        return next((row for row in series[metric] if row.period_end == period), None)

    latest = periods_found[-1] if periods_found else None
    previous = periods_found[-2] if len(periods_found) > 1 else None

    # Quarterly comparisons use the same quarter a year earlier to reduce seasonal noise.
    if period_type == "quarterly" and latest:
        target = latest.replace(year=latest.year - 1)
        previous = next((p for p in periods_found if p == target), None)
    older = periods_found[periods_found.index(previous) - 1] if previous in periods_found and periods_found.index(previous) > 0 else None

    def val(metric: str, period: date | None) -> float | None:
        row = get(metric, period)
        return float(row.normalised_value_in_crore) if row else None

    earnings: list[dict] = []
    rev, pat, pbt, ebitda = (get(key, latest) for key in ("revenue", "pat", "pbt", "ebitda"))
    prev_rev, prev_pat, prev_pbt, prev_ebitda = (get(key, previous) for key in ("revenue", "pat", "pbt", "ebitda"))
    if rev and pat and prev_rev and prev_pat:
        revenue_growth = (float(rev.normalised_value_in_crore) / float(prev_rev.normalised_value_in_crore) - 1) * 100 if prev_rev.normalised_value_in_crore > 0 else None
        pat_growth = (float(pat.normalised_value_in_crore) / float(prev_pat.normalised_value_in_crore) - 1) * 100 if prev_pat.normalised_value_in_crore > 0 else None
        divergence = "Revenue increased, but PAT declined" if revenue_growth is not None and pat_growth is not None and revenue_growth > MATERIAL_CHANGE_PCT and pat_growth < -MATERIAL_CHANGE_PCT else "PAT growth outpaced revenue growth" if revenue_growth is not None and pat_growth is not None and pat_growth - revenue_growth > MATERIAL_CHANGE_PCT else "Revenue and PAT growth moved broadly together" if revenue_growth is not None and pat_growth is not None else "Growth comparison is unavailable because a prior value is not positive."
        prior_revenue_growth = (float(prev_rev.normalised_value_in_crore / get("revenue", older).normalised_value_in_crore - 1) * 100) if older and get("revenue", older) and get("revenue", older).normalised_value_in_crore > 0 else None
        prior_pat_growth = (float(prev_pat.normalised_value_in_crore / get("pat", older).normalised_value_in_crore - 1) * 100) if older and get("pat", older) and get("pat", older).normalised_value_in_crore > 0 else None
        divergence_now = pat_growth - revenue_growth if revenue_growth is not None and pat_growth is not None else None
        divergence_prior = prior_pat_growth - prior_revenue_growth if prior_pat_growth is not None and prior_revenue_growth is not None else None
        if divergence_now is None: status = "insufficient_data"
        elif divergence_now < -MATERIAL_CHANGE_PCT and (divergence_prior is None or divergence_now < divergence_prior - MATERIAL_CHANGE_PCT): status = "watch"
        elif divergence_prior is not None and divergence_now > divergence_prior + MATERIAL_CHANGE_PCT: status = "improving"
        elif divergence_prior is not None and divergence_now < divergence_prior - MATERIAL_CHANGE_PCT: status = "deteriorating"
        else: status = "no_material_change"
        earnings.append(_signal("revenue_pat_growth", "Revenue versus PAT growth", status,
            f"Revenue changed {revenue_growth:+.1f}% and PAT changed {pat_growth:+.1f}% versus {period_label(previous)}. {divergence}.",
            "PAT growth minus Revenue from Operations growth; quarterly compares the same quarter year-on-year",
            divergence_now, divergence_prior, period_label(latest), period_label(older),
            [item for item in (get("revenue", older), prev_rev, rev, get("pat", older), prev_pat, pat) if item], "pp"))
    else:
        earnings.append(_signal("revenue_pat_growth", "Revenue versus PAT growth", "insufficient_data",
            "At least two validated, comparable periods of both revenue and PAT are required.", None))

    for key, label, row, prior in (("pbt_margin", "PBT margin", pbt, rev), ("pat_margin", "PAT margin", pat, rev), ("ebitda_margin", "EBITDA margin", ebitda, rev)):
        prior_metric = {"pbt_margin": prev_pbt, "pat_margin": prev_pat, "ebitda_margin": prev_ebitda}[key]
        current_value = float(row.normalised_value_in_crore / prior.normalised_value_in_crore * 100) if row and prior and prior.normalised_value_in_crore else None
        previous_value = float(prior_metric.normalised_value_in_crore / prev_rev.normalised_value_in_crore * 100) if prior_metric and prev_rev and prev_rev.normalised_value_in_crore else None
        if row and prior:
            diff = current_value - previous_value if previous_value is not None else None
            status = "insufficient_data" if diff is None else "improving" if diff > MATERIAL_CHANGE_PCT else "deteriorating" if diff < -MATERIAL_CHANGE_PCT else "no_material_change"
            evidence = [item for item in (prior_metric, prev_rev, row, prior) if item]
            explain = f"{label} is {current_value:.1f}%" + (f", versus {previous_value:.1f}% in {period_label(previous)}." if previous_value is not None else ".")
            earnings.append(_signal(key, label, status, explain, f"{label.split()[0]} / Revenue from Operations × 100", current_value, previous_value, period_label(latest), period_label(previous), evidence, "%"))
        else:
            reason = "EBITDA is not separately disclosed in validated source observations." if key == "ebitda_margin" and not row else "Validated revenue and profit observations for this period are incomplete."
            earnings.append(_signal(key, label, "insufficient_data", reason, None, unit="%"))
    signal_groups.append({"code": "earnings_quality", "name": "Earnings quality", "signals": earnings,
                         "coverage_state": "available" if any(s["current_value"] is not None for s in earnings) else "insufficient_data"})

    cash_signals = []
    ocf, capex = get("cash_from_operations", latest), get("capital_expenditure_ppe", latest)
    prior_ocf, prior_capex = get("cash_from_operations", previous), get("capital_expenditure_ppe", previous)
    if ocf and pat and pat.normalised_value_in_crore > 0:
        ratio = float(ocf.normalised_value_in_crore / pat.normalised_value_in_crore)
        prior_ratio = float(prior_ocf.normalised_value_in_crore / prev_pat.normalised_value_in_crore) if prior_ocf and prev_pat and prev_pat.normalised_value_in_crore > 0 else None
        cash_signals.append(_signal("cash_conversion", "Operating cash flow / PAT", _change_status(ratio, prior_ratio),
            f"Operating cash flow is {ratio:.2f}× PAT" + (f", compared with {prior_ratio:.2f}× in {period_label(previous)}." if prior_ratio is not None else "."),
            "operating cash flow / PAT", ratio, prior_ratio, period_label(latest), period_label(previous), [ocf, pat] + ([prior_ocf, prev_pat] if prior_ratio is not None else []), "x"))
    else:
        cash_signals.append(_signal("cash_conversion", "Operating cash flow / PAT", "insufficient_data",
            "Validated operating cash flow and positive PAT for the same period are required.", "operating cash flow / PAT"))
    if ocf and capex:
        fcf = float(ocf.normalised_value_in_crore + capex.normalised_value_in_crore)
        prior_fcf = float(prior_ocf.normalised_value_in_crore + prior_capex.normalised_value_in_crore) if prior_ocf and prior_capex else None
        fcf_status = "insufficient_data" if prior_fcf is None else "improving" if fcf > prior_fcf + MATERIAL_CHANGE_CRORE else "deteriorating" if fcf < prior_fcf - MATERIAL_CHANGE_CRORE else "no_material_change"
        cash_signals.append(_signal("free_cash_flow", "Free cash flow", fcf_status,
            f"Free cash flow is INR {fcf:,.0f} crore" + (f", compared with INR {prior_fcf:,.0f} crore in {period_label(previous)}." if prior_fcf is not None else "."),
            "operating cash flow + source-reported capex cash-flow amount (preserve original sign)", fcf, prior_fcf, period_label(latest), period_label(previous), [ocf, capex] + ([prior_ocf, prior_capex] if prior_fcf is not None else [])))
    else:
        cash_signals.append(_signal("free_cash_flow", "Free cash flow", "insufficient_data",
            "Validated operating cash flow and separately identified capex observations for the same period are required.", "operating cash flow + reported capex cash-flow amount"))
    signal_groups.append({"code": "cash_conversion", "name": "Cash conversion", "signals": cash_signals,
                         "coverage_state": "available" if any(s["current_value"] is not None for s in cash_signals) else "insufficient_data"})

    balance_signals = []
    # Match exact dates and source basis for each derived balance-sheet indicator.
    def derived_balance(period: date | None):
        n, c, cash, equity, ca, cl = (get(m, period) for m in ("borrowings_noncurrent", "borrowings_current", "cash_and_cash_equivalents", "total_equity", "current_assets", "current_liabilities"))
        if n and c and cash:
            net = float(n.normalised_value_in_crore + c.normalised_value_in_crore - cash.normalised_value_in_crore)
            net_rows = [n, c, cash]
        else: net, net_rows = None, [r for r in (n, c, cash) if r]
        debt_eq = float((n.normalised_value_in_crore + c.normalised_value_in_crore) / equity.normalised_value_in_crore) if n and c and equity and equity.normalised_value_in_crore > 0 else None
        debt_rows = [r for r in (n, c, equity) if r]
        ratio = float(ca.normalised_value_in_crore / cl.normalised_value_in_crore) if ca and cl and cl.normalised_value_in_crore > 0 else None
        ratio_rows = [r for r in (ca, cl) if r]
        return (net, net_rows), (debt_eq, debt_rows), (ratio, ratio_rows)
    now_balance, prior_balance = derived_balance(latest), derived_balance(previous)
    for i, (code, name, unit, formula, inverse) in enumerate((
        ("net_debt", "Net debt (lease liabilities excluded)", "INR crore", "non-current borrowings + current borrowings - cash and cash equivalents; leases excluded", True),
        ("debt_equity", "Borrowings / equity", "x", "(non-current borrowings + current borrowings) / total equity; leases excluded", True),
        ("current_ratio", "Current ratio", "x", "current assets / current liabilities", False),
    )):
        current_val, current_rows = now_balance[i]
        prev_val, prev_rows = prior_balance[i]
        status = _change_status(current_val, prev_val, higher_is_better=not inverse)
        if code == "net_debt" and current_val is not None and prev_val is not None:
            status = "improving" if current_val < prev_val - MATERIAL_CHANGE_CRORE else "deteriorating" if current_val > prev_val + MATERIAL_CHANGE_CRORE else "no_material_change"
        all_evidence = prev_rows + current_rows if prev_val is not None else current_rows
        desc = f"Latest value is {current_val:.2f} {unit}" + (f", versus {prev_val:.2f} in {period_label(previous)}." if prev_val is not None else "; no comparable validated prior period is available.") if current_val is not None else "Required validated balance-sheet inputs for this period are incomplete."
        balance_signals.append(_signal(code, name, status, desc, formula, current_val, prev_val, period_label(latest), period_label(previous), all_evidence, unit))
    signal_groups.append({"code": "balance_sheet", "name": "Balance-sheet resilience", "signals": balance_signals,
                         "coverage_state": "available" if any(s["current_value"] is not None for s in balance_signals) else "insufficient_data"})

    # Working-capital and governance are not asserted unless their evidence is present.
    working = [_signal("working_capital_cycle", "Working-capital cycle", "insufficient_data",
        "Inventory, receivables, payables and compatible cost-base observations have not been validated for comparable periods.", None)]
    signal_groups.append({"code": "working_capital", "name": "Working capital", "signals": working, "coverage_state": "insufficient_data"})
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    disclosures = db.scalars(select(Announcement).where(
        Announcement.company_id == company.id,
        Announcement.announcement_datetime >= cutoff.replace(tzinfo=None),
    ).order_by(Announcement.announcement_datetime.desc()).limit(200)).all()
    tagged = []
    for item in disclosures:
        classification = classify_announcement(item)
        if classification["category"] in {"capital", "governance", "ma_restructuring", "corporate_action", "management"}:
            tagged.append({"id": item.id, "date": item.announcement_datetime.isoformat() if item.announcement_datetime else None,
                           "subject": item.subject, "category": classification["category"],
                           "materiality": classification["materiality"], "url": item.attachment_url or item.source_page_url})
    governance_signals = [_signal("recent_disclosures", "Recent capital and governance disclosures", "watch" if tagged else "insufficient_data",
        f"{len(tagged)} exchange disclosures matched FINN's capital/governance categories in the last 12 months. These are triage matches, not findings that a disclosure is new, adverse or material.",
        None, current=float(len(tagged)), unit="disclosures")]
    signal_groups.append({"code": "governance_capital", "name": "Governance & capital allocation", "signals": governance_signals,
        "coverage_state": "available" if tagged else "insufficient_data", "disclosures": tagged[:8]})

    available = sum(group["coverage_state"] == "available" for group in signal_groups)
    latest_period = period_label(latest)
    return {
        "company": company.name, "ticker": company.ticker, "statement_basis": basis,
        "period_type": period_type, "periods_requested": periods, "latest_period": latest_period,
        "coverage": {"available_groups": available, "total_groups": len(signal_groups),
            "validated_observation_count": sum(len(rows) for rows in series.values()),
            "coverage_state": "available" if available else "insufficient_data",
            "limitations": ["Working-capital cycle is unavailable until its required annual inputs are validated.",
                "Governance disclosure category matches require analyst review before interpretation." ]},
        "signal_groups": signal_groups,
        "what_changed": [signal for group in signal_groups for signal in group["signals"] if signal["status"] in {"improving", "deteriorating", "watch", "new_disclosure"}],
        "rule_version": RULE_VERSION,
    }
