"""Copilot: one ask box for the whole app.

Routing (cheap, deterministic): if the question names a company (ticker or a distinctive word of its
name), or the user is on that company's page, it is answered from that company's indexed primary
documents (existing evidence-grounded research assistant). Otherwise it is answered across the
portfolio from the last N days of material filings and triggered alerts, citing the filings it used.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import llm
from .intelligence import classify_announcement
from .models import AlertEvent, Announcement, Company, PortfolioHolding
from .research_assistant import answer_question

PROMPT_VERSION = "copilot-portfolio-v1"
STOP = {"limited", "ltd", "india", "industries", "company", "corporation", "bank", "the", "and", "of", "services", "enterprises", "technologies"}
SYSTEM = """You are FINN, an investment copilot for a portfolio manager. Answer ONLY from the filings and alerts
listed (Indian exchange announcements with FINN's one-line summaries). Be concise: max 110 words, lead with the
answer, then the 2-4 items that support it. Cite items by their [A..] or [E..] ids inline. If the list cannot
answer the question, say so plainly. No investment advice. Return ONLY JSON: {"answer": "...", "used": ["A12", ...]}"""


def resolve_company(db: Session, question: str, ticker: str | None) -> Company | None:
    companies = db.scalars(select(Company)).all()
    text = f" {question.lower()} "
    for company in companies:
        if re.search(rf"(?<![a-z0-9]){re.escape(company.ticker.lower())}(?![a-z0-9])", text):
            return company
    for company in companies:
        words = [w for w in re.findall(r"[a-z]+", company.name.lower()) if len(w) >= 4 and w not in STOP]
        if words and words[0] in text:
            return company
    if ticker:
        return next((c for c in companies if c.ticker.upper() == ticker.upper()), None)
    return None


def _portfolio_context(db: Session, days: int) -> tuple[list[str], dict[str, dict]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.scalars(select(Announcement).join(Company).join(PortfolioHolding).where(PortfolioHolding.is_active.is_(True))
                      .order_by(Announcement.announcement_datetime.desc())).all()
    lines, refs = [], {}
    for item in rows:
        stamp = item.announcement_datetime
        if stamp is None or (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)) < since:
            continue
        c = classify_announcement(item)
        if c["materiality"] == "routine":
            continue
        ref = f"A{item.id}"
        lines.append(f"[{ref}] {stamp.date()} {item.company.name} ({item.company.ticker}) | {c['category_label']} | {c['materiality']} | {c.get('summary') or item.subject}")
        refs[ref] = {"label": f"{item.company.ticker} · {c['category_label']} · {stamp.date()}", "url": item.attachment_url or item.source_page_url,
                     "ticker": item.company.ticker}
        if len(lines) >= 90:
            break
    events = db.scalars(select(AlertEvent).order_by(AlertEvent.triggered_at.desc()).limit(40)).all()
    for event in events:
        stamp = event.triggered_at if event.triggered_at.tzinfo else event.triggered_at.replace(tzinfo=timezone.utc)
        if stamp < since:
            continue
        ref = f"E{event.id}"
        lines.append(f"[{ref}] {stamp.date()} {event.company.name} ({event.company.ticker}) | alert {event.rule_type} | {event.reason}")
        refs[ref] = {"label": f"{event.company.ticker} · {event.rule_type.replace('_', ' ')} · {stamp.date()}",
                     "url": event.announcement.attachment_url if event.announcement else None, "ticker": event.company.ticker}
    return lines, refs


def ask(db: Session, question: str, ticker: str | None = None, days: int = 14) -> dict:
    q = (question or "").strip()
    if not q or len(q) > 500:
        return {"status": "abstained", "answer": "Ask a question of up to 500 characters.", "citations": [], "scope": "none"}
    company = resolve_company(db, q, ticker)
    if company is not None:
        result = answer_question(db, company.ticker, q)
        if result.get("status") in {"abstained", "partial"} and llm.is_configured():
            # Deterministic routes are narrow; fall back to retrieval over the company's indexed documents.
            from .llm_research import answer_with_provider
            retrieved = answer_with_provider(db, company, q)
            if retrieved and retrieved.get("status") == "available":
                result = retrieved
        return {**result, "scope": "company", "ticker": company.ticker, "company": company.name}
    lines, refs = _portfolio_context(db, days)
    if not lines:
        return {"status": "abstained", "answer": f"No material portfolio filings or alerts in the last {days} days to answer from.", "citations": [], "scope": "portfolio"}
    def listing(prefix: str) -> dict:
        top = lines[:5]
        return {"status": "available", "scope": "portfolio", "mode": "list",
                "answer": prefix + "\n" + "\n".join("• " + t.split("] ", 1)[1].split(" | ", 1)[0] + " — " + t.rsplit(" | ", 1)[-1] for t in top),
                "citations": [{"ref": t[1:t.index("]")], **refs[t[1:t.index("]")]]} for t in top]}
    if not llm.is_configured():
        return listing("No model is configured, so here are the latest material items:")
    try:
        reply, meta = llm.complete_json(db, tier="fast", purpose="copilot", prompt_version=PROMPT_VERSION, system=SYSTEM, max_tokens=900,
                                        user=f"Question: {q}\n\nItems (last {days} days):\n" + "\n".join(lines))
    except llm.LLMUnavailable:
        return listing("The model is busy right now, so here are the latest material items instead:")
    used = [u for u in (reply.get("used") or []) if isinstance(u, str) and u in refs]
    cited_inline = re.findall(r"\[([AE]\d+)\]", str(reply.get("answer") or ""))
    used += [u for u in cited_inline if u in refs and u not in used]
    answer = re.sub(r"\[([AE]\d+)\]", lambda m: f"[{m.group(1)}]" if m.group(1) in refs else "", str(reply.get("answer") or "")).strip()
    return {"status": "available" if answer else "abstained", "answer": answer or "No answer returned.", "scope": "portfolio",
            "citations": [{"ref": u, **refs[u]} for u in used[:6]], "model": meta.get("model")}
