"""Deterministic, evidence-grounded answers for supported FINN research questions."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Announcement, Company, FinancialObservation, ManagementStatement
from .llm_research import answer_with_provider


def _annual(db: Session, company_id: int, metric: str, basis: str = "consolidated"):
    return db.scalars(select(FinancialObservation).where(
        FinancialObservation.company_id == company_id,
        FinancialObservation.canonical_metric == metric,
        FinancialObservation.statement_basis == basis,
        FinancialObservation.period_type == "annual",
        FinancialObservation.extraction_status == "validated",
    ).order_by(FinancialObservation.period_end)).all()


def _quarterly(db: Session, company_id: int, metric: str, basis: str = "consolidated"):
    rows = db.scalars(select(FinancialObservation).where(
        FinancialObservation.company_id == company_id,
        FinancialObservation.canonical_metric == metric,
        FinancialObservation.statement_basis == basis,
        FinancialObservation.period_type != "annual",
        FinancialObservation.extraction_status == "validated",
    ).order_by(FinancialObservation.period_end)).all()
    # A later filing can repeat a comparative period; retain one primary observation per period.
    by_period = {}
    for row in rows:
        key = (row.period_end, row.period_start)
        by_period[key] = row
    return sorted(by_period.values(), key=lambda row: row.period_end)[-4:]


def _cite(row: FinancialObservation) -> dict:
    doc = row.document
    return {"title": doc.title, "page": row.page_number, "row": row.row_label,
            "period": row.fiscal_year if row.period_type == "annual" else f"{row.fiscal_quarter} {row.fiscal_year}",
            "basis": row.statement_basis, "value": row.reported_value_raw,
            "unit": row.reported_unit, "url": f"/api/documents/{doc.id}/file#page={row.page_number}"}


def answer_question(db: Session, ticker: str, question: str) -> dict:
    q = question.strip()
    if not q or len(q) > 500:
        return {"status": "abstained", "answer": "Enter a question up to 500 characters.", "citations": [], "mode": "deterministic_evidence"}
    company = db.scalar(select(Company).where(func.upper(Company.ticker) == ticker.upper()))
    if company is None:
        return {"status": "unavailable", "answer": f"FINN has no stored research profile for {ticker.upper()}.", "citations": [], "mode": "deterministic_evidence"}
    low = q.lower()
    if any(word in low for word in ("revenue", "sales")) and any(word in low for word in ("year", "annual", "three", "changed", "trend")):
        rows = _annual(db, company.id, "revenue")
        if len(rows) < 2:
            return _abstain("FINN does not have at least two validated annual revenue observations for this company.")
        text = []
        for prior, current in zip(rows, rows[1:]):
            delta = (float(current.normalised_value_in_crore) / float(prior.normalised_value_in_crore) - 1) * 100 if prior.normalised_value_in_crore else None
            text.append(f"Revenue from Operations was ₹{float(prior.normalised_value_in_crore):,.2f} crore in {prior.fiscal_year} and ₹{float(current.normalised_value_in_crore):,.2f} crore in {current.fiscal_year} ({delta:+.1f}% year over year).")
        return _result(" ".join(text), rows, "annual revenue growth uses reported Revenue from Operations and consecutive validated annual periods.")
    if "cash" not in low and any(word in low for word in ("profit", "profitability", "pat", "pbt", "quarter")):
        rows = _quarterly(db, company.id, "pat")
        pbt = {row.period_end: row for row in _quarterly(db, company.id, "pbt")}
        if len(rows) < 2:
            return _abstain("FINN does not have enough validated quarterly PAT observations to compare profitability.")
        lines=[]
        citations=[]
        for row in rows:
            peer=pbt.get(row.period_end)
            lines.append(f"{row.fiscal_quarter} {row.fiscal_year}: PAT ₹{float(row.normalised_value_in_crore):,.2f} crore" + (f", PBT ₹{float(peer.normalised_value_in_crore):,.2f} crore." if peer else "."))
            citations.append(row)
            if peer: citations.append(peer)
        return _result("Quarterly profitability from the latest validated periods: " + " ".join(lines), citations, "quarterly PAT and PBT are shown as reported; FINN does not annualise quarter values.")
    if any(word in low for word in ("borrow", "debt", "net debt")):
        series={metric:_annual(db,company.id,metric) for metric in ("borrowings_noncurrent","borrowings_current","cash_and_cash_equivalents")}
        common=sorted(set.intersection(*(set(r.period_end for r in values) for values in series.values())))
        if len(common)<1: return _abstain("Validated annual borrowings and cash observations are not available for a net-debt comparison.")
        citations=[]; lines=[]
        for period in common:
            items=[next(r for r in series[m] if r.period_end==period) for m in series]
            debt=float(items[0].normalised_value_in_crore+items[1].normalised_value_in_crore)
            cash=float(items[2].normalised_value_in_crore)
            lines.append(f"FY ending {period}: borrowings ₹{debt:,.2f} crore, cash ₹{cash:,.2f} crore, and net debt ₹{debt-cash:,.2f} crore (lease liabilities excluded).")
            citations.extend(items)
        return _result(" ".join(lines),citations,"net debt = non-current borrowings + current borrowings − cash and cash equivalents; leases excluded.")
    if "cash" in low and ("pat" in low or "conversion" in low or "pace" in low):
        ocf={r.period_end:r for r in _annual(db,company.id,"cash_from_operations")}
        pat={r.period_end:r for r in _annual(db,company.id,"pat")}
        common=sorted(set(ocf)&set(pat))
        if not common: return _abstain("FINN has no matching validated annual operating cash flow and PAT observations.")
        citations=[]; lines=[]
        for period in common:
            o,p=ocf[period],pat[period]
            ratio=float(o.normalised_value_in_crore/p.normalised_value_in_crore) if p.normalised_value_in_crore>0 else None
            lines.append(f"FY ending {period}: operating cash flow ₹{float(o.normalised_value_in_crore):,.2f} crore versus PAT ₹{float(p.normalised_value_in_crore):,.2f} crore"+(f"; OCF/PAT was {ratio:.2f}x." if ratio is not None else "; OCF/PAT is withheld because PAT is not positive."))
            citations.extend([o,p])
        return _result(" ".join(lines),citations,"cash conversion is defined as operating cash flow divided by reported PAT; a non-positive PAT is not treated as an ordinary positive ratio.")
    if any(word in low for word in ("management", "growth", "afterwards", "guidance", "said")):
        items=db.scalars(select(ManagementStatement).where(ManagementStatement.company_id==company.id,ManagementStatement.review_status=="approved").order_by(ManagementStatement.target_period)).all()
        financial_metrics={"revenue","pbt","pat","ebitda","margin","capex","net_debt_to_ebitda"}
        financial=[item for item in items if item.metric_name in financial_metrics and item.target_low is not None and item.target_period]
        if not financial:
            return _abstain("The reviewed transcript set has no approved, period-specific financial growth guidance that can be matched to a later financial actual. FINN will not substitute qualitative commentary or an unrelated result.")
        return {"status":"partial","answer":"FINN found approved quantitative guidance candidates, but a comparable actual is not yet stored for the same metric and target period.","citations":[{"title":x.document.title,"page":x.page_number,"row":x.exact_quote,"period":x.target_period,"basis":"management statement","url":f"/api/documents/{x.document_id}/file#page={x.page_number}"} for x in financial],"formula":None,"mode":"deterministic_evidence"}
    if any(word in low for word in ("developments", "positive", "negative", "important")):
        items=db.scalars(select(Announcement).where(Announcement.company_id==company.id).order_by(Announcement.announcement_datetime.desc()).limit(6)).all()
        if not items: return _abstain("No persisted company announcements are available to assess recent developments.")
        citations=[{"title":x.subject,"page":None,"row":x.details or x.subject,"period":x.announcement_datetime.isoformat() if x.announcement_datetime else None,"basis":"exchange announcement","url":x.attachment_url or x.source_page_url} for x in items]
        answer="FINN found these recent persisted announcements; they are listed as source events and have not been independently labelled positive or negative: " + "; ".join(x.subject for x in items)
        return {"status":"available","answer":answer,"citations":citations,"formula":None,"mode":"deterministic_evidence"}
    model_answer = answer_with_provider(db, company, q)
    if model_answer is not None:
        return model_answer
    return _abstain("This question is outside FINN's supported deterministic queries, and no LLM provider is configured. Try annual revenue, quarterly profitability, borrowings/net debt, operating cash flow versus PAT, management guidance, or recent announcements.")


def _result(answer, rows, formula):
    citations=[]
    seen=set()
    for row in rows:
        key=(row.document_id,row.page_number,row.canonical_metric,row.period_end)
        if key not in seen: citations.append(_cite(row)); seen.add(key)
    return {"status":"available","answer":answer,"citations":citations,"formula":formula,"mode":"deterministic_evidence"}


def _abstain(answer):
    return {"status":"abstained","answer":answer,"citations":[],"formula":None,"mode":"deterministic_evidence"}
