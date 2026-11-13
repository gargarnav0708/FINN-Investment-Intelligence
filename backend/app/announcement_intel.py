"""Announcement intelligence: taxonomy, one-line summaries and management-change profiles.

* Classification: announcements are sent to the fast model in batches of 20 (subject + exchange
  description only), which returns the reference-taxonomy category, a materiality level and - only
  for material items - a one-line summary. Keyword rules are the fallback when no model is
  configured or a batch fails. Routine items get no summary: they are surfaced as the filing itself.
* Management changes: the appointment / resignation filing must carry a brief profile of the person
  (SEBI LODR Reg. 30). The PDF is read by the fast model, and the profile is kept only if it is
  found (near-)verbatim in the filing. Optional web enrichment (Tavily or Serper) adds a short
  professional background, written only from the search snippets that name the person, with links.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import llm
from .intelligence import TAXONOMY, keyword_category, keyword_materiality
from .models import Announcement, AnnouncementInsight, Company, DocumentPage, PortfolioHolding, SourceDocument

log = logging.getLogger("finn.announcements")
CLASSIFY_VERSION = "announce-classify-v1"
PEOPLE_VERSION = "mgmt-profile-v1"
BACKGROUND_VERSION = "mgmt-background-v1"
BATCH = 20

CLASSIFY_SYSTEM = """You triage Indian stock-exchange corporate announcements for a portfolio manager.
For each item return the category (one key from the taxonomy), a materiality level and, ONLY when
materiality is high or medium, a one-line summary (max 22 words) that says what happened and the number
that matters (amount, %, rating, name, date) if the text gives one. Use only the text given; never guess.
Materiality: high = could move the stock or change the investment case; medium = worth knowing;
routine = compliance paperwork. Return ONLY JSON."""

CLASSIFY_USER = """Taxonomy (key: meaning):
{taxonomy}

Items:
{items}

Return {{"items": [{{"id": <id>, "category": "<key>", "materiality": "high"|"medium"|"routine",
"summary": "<one line or null>", "reason": "<why it matters, max 12 words, or null>"}}]}}"""

PEOPLE_SYSTEM = """You read Indian listed-company disclosures about changes in directors / key managerial personnel.
Extract every person whose appointment, re-appointment, resignation, retirement or cessation is disclosed.
"profile" must be the brief profile text COPIED VERBATIM from the filing (max 90 words), or null if the
filing has none. Never write a profile yourself. Return ONLY JSON."""

PEOPLE_USER = """Company: {company}
Filing pages (with [[PAGE n]] markers):
{text}

Return {{"people": [{{"name": "...", "role": "...", "change": "appointed"|"re-appointed"|"resigned"|"retired"|"ceased",
"effective_date": "YYYY-MM-DD or null", "reason": "<stated reason or null>", "profile": "<verbatim or null>", "page": <int>}}]}}"""

BACKGROUND_SYSTEM = """Write a short professional background (max 45 words) of the named person using ONLY the
search results provided. Use a result only if it clearly refers to this person (same name AND a matching
company, role or industry). If none do, return null. Return ONLY JSON."""


def _taxonomy_text() -> str:
    from .intelligence import TAXONOMY as tax
    return "\n".join(f"- {k}: {label}. {why}" for k, (label, _, why) in tax.items())


def _keyword_insight(item: Announcement) -> dict:
    category = keyword_category(item)
    return {"category": category, "materiality": keyword_materiality(item, category), "summary": None, "reason": None, "method": "keyword"}


def classify_batch(db: Session, items: list[Announcement]) -> dict[int, dict]:
    """Return {announcement_id: insight dict}. Falls back to keyword rules per item."""
    results = {item.id: _keyword_insight(item) for item in items}
    if not items or not llm.is_configured():
        return results
    lines = []
    for item in items:
        details = " ".join((item.details or "").split())[:600]
        lines.append(f"[{item.id}] {item.company.name} | NSE category: {item.original_category or '-'} | Subject: {item.subject} | Text: {details}")
    try:
        reply, meta = llm.complete_json(db, tier="fast", purpose="announcement_classify", prompt_version=CLASSIFY_VERSION,
                                        system=CLASSIFY_SYSTEM, max_tokens=4000,
                                        user=CLASSIFY_USER.format(taxonomy=_taxonomy_text(), items="\n".join(lines)))
    except llm.LLMUnavailable as exc:
        log.warning("classification batch fell back to keywords: %s", str(exc)[:200])
        return results
    for row in reply.get("items") or []:
        try:
            ident = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if ident not in results or row.get("category") not in TAXONOMY:
            continue
        materiality = row.get("materiality") if row.get("materiality") in {"high", "medium", "routine"} else TAXONOMY[row["category"]][1]
        summary = (str(row.get("summary")).strip() or None) if row.get("summary") and materiality != "routine" else None
        results[ident] = {"category": row["category"], "materiality": materiality, "summary": summary[:300] if summary else None,
                          "reason": (str(row.get("reason")).strip()[:200] or None) if row.get("reason") else None,
                          "method": "llm", "model": meta.get("model")}
    return results


def _store(db: Session, item: Announcement, data: dict) -> AnnouncementInsight:
    insight = db.scalar(select(AnnouncementInsight).where(AnnouncementInsight.announcement_id == item.id))
    if insight is None:
        insight = AnnouncementInsight(announcement_id=item.id, category=data["category"], materiality=data["materiality"], method=data["method"])
        db.add(insight)
    insight.category, insight.materiality, insight.method = data["category"], data["materiality"], data["method"]
    insight.summary, insight.reason, insight.model = data.get("summary"), data.get("reason"), data.get("model")
    return insight


def _attachment_pages(db: Session, company: Company, item: Announcement) -> tuple[SourceDocument | None, dict[int, str]]:
    from .services import _register_exchange_documents

    if not item.attachment_url:
        return None, {}
    document = db.scalar(select(SourceDocument).where(SourceDocument.company_id == company.id, SourceDocument.download_url == item.attachment_url))
    if document is None or not document.local_file_path:
        _register_exchange_documents(db, company, [{"subject": item.subject, "details": item.details, "attachment_url": item.attachment_url,
                                                    "source_page_url": item.source_page_url, "announcement_datetime": item.announcement_datetime}],
                                     force_type="management_change")
        document = db.scalar(select(SourceDocument).where(SourceDocument.company_id == company.id, SourceDocument.download_url == item.attachment_url))
    if document is None:
        return None, {}
    pages = {p.page_number: p.extracted_text for p in db.scalars(select(DocumentPage).where(DocumentPage.document_id == document.id)).all()}
    return document, pages


def web_search(query: str, limit: int = 5) -> list[dict]:
    """Tavily or Serper, whichever key is configured. Returns [{title, url, snippet}]."""
    tavily, serper = os.getenv("TAVILY_API_KEY", "").strip(), os.getenv("SERPER_API_KEY", "").strip()
    try:
        if tavily:
            r = requests.post("https://api.tavily.com/search", json={"api_key": tavily, "query": query, "max_results": limit, "search_depth": "basic"}, timeout=20)
            r.raise_for_status()
            return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")[:600]} for x in r.json().get("results", [])][:limit]
        if serper:
            r = requests.post("https://google.serper.dev/search", json={"q": query, "num": limit, "gl": "in"}, headers={"X-API-KEY": serper}, timeout=20)
            r.raise_for_status()
            return [{"title": x.get("title", ""), "url": x.get("link", ""), "snippet": x.get("snippet", "")[:600]} for x in r.json().get("organic", [])][:limit]
    except Exception as exc:
        log.warning("web search failed: %s", exc)
    return []


def search_configured() -> bool:
    return bool(os.getenv("TAVILY_API_KEY", "").strip() or os.getenv("SERPER_API_KEY", "").strip())


def _background(db: Session, person: dict, company: Company) -> dict:
    if not search_configured() or not person.get("name"):
        return {}
    surname = person["name"].split()[-1].lower()
    results = [r for r in web_search(f"\"{person['name']}\" {company.name} LinkedIn") if surname in (r["title"] + r["snippet"]).lower()]
    if not results:
        return {}
    listing = "\n".join(f"[{i}] {r['title']} | {r['url']}\n{r['snippet']}" for i, r in enumerate(results))
    try:
        reply, _ = llm.complete_json(db, tier="fast", purpose="mgmt_background", prompt_version=BACKGROUND_VERSION, system=BACKGROUND_SYSTEM, max_tokens=600,
                                     user=f"Person: {person['name']}, {person.get('role') or ''} at {company.name}\n\nSearch results:\n{listing}\n\n"
                                          'Return {"background": "<text or null>", "used": [<result numbers>]}')
    except llm.LLMUnavailable:
        return {}
    text = reply.get("background")
    if not text:
        return {}
    used = []
    for index in reply.get("used") or []:
        try:
            used.append(results[int(index)])
        except (ValueError, TypeError, IndexError):
            continue
    if not used:
        return {}
    return {"background": str(text)[:400], "background_sources": [{"title": r["title"][:120], "url": r["url"]} for r in used[:3]]}


def extract_people(db: Session, item: Announcement) -> list[dict]:
    from .guidance import _ground

    company = item.company
    document, pages = _attachment_pages(db, company, item)
    source_text = pages if pages else ({0: item.details or ""} if item.details else {})
    if not source_text or not llm.is_configured():
        return []
    text = "\n".join(f"[[PAGE {n}]]\n{source_text[n]}" for n in sorted(source_text))[:40000]
    try:
        reply, meta = llm.complete_json(db, tier="fast", purpose="mgmt_profile", prompt_version=PEOPLE_VERSION, system=PEOPLE_SYSTEM,
                                        max_tokens=2500, user=PEOPLE_USER.format(company=company.name, text=text))
    except llm.LLMUnavailable:
        return []
    people = []
    whole = " ".join(source_text.values()).lower()
    for row in reply.get("people") or []:
        name = " ".join(str(row.get("name") or "").split())
        if len(name) < 4 or name.split()[-1].lower() not in whole:
            continue  # the person must actually be named in the filing
        try:
            hint = int(row.get("page")) if row.get("page") is not None else None
        except (TypeError, ValueError):
            hint = None
        profile, page = None, hint
        if row.get("profile"):
            page, profile = _ground(str(row["profile"]), hint, source_text)
        person = {"name": name, "role": (str(row.get("role") or "").strip() or None), "change": row.get("change"),
                  "effective_date": row.get("effective_date"), "reason": row.get("reason"),
                  "profile": profile, "profile_page": page if profile else None,
                  "source_url": (f"/api/documents/{document.id}/file#page={page}" if document and page else item.attachment_url)}
        person.update(_background(db, person, company))
        people.append(person)
    return people[:4]


def enrich_announcements(db: Session, days: int = 30, company_ids: list[int] | None = None, reclassify: bool = False, profiles: bool = True) -> dict:
    """Classify + summarise announcements in the window; profile people in management changes."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    query = select(Announcement).join(Company)
    if company_ids:
        query = query.where(Announcement.company_id.in_(company_ids))
    else:
        query = query.join(PortfolioHolding).where(PortfolioHolding.is_active.is_(True))
    rows = [a for a in db.scalars(query.order_by(Announcement.announcement_datetime.desc())).all()
            if a.announcement_datetime is None or (a.announcement_datetime if a.announcement_datetime.tzinfo else a.announcement_datetime.replace(tzinfo=timezone.utc)) >= since]
    pending = [a for a in rows if reclassify or a.insight is None or (a.insight.method == "keyword" and llm.is_configured())]
    classified = 0
    for start in range(0, len(pending), BATCH):
        chunk = pending[start:start + BATCH]
        decisions = classify_batch(db, chunk)
        for item in chunk:
            _store(db, item, decisions[item.id])
            classified += 1
        db.commit()
    profiled = 0
    if profiles and llm.is_configured():
        for item in rows:
            insight = item.insight
            if insight is None or insight.category != "management_change" or insight.materiality == "routine" or insight.people_status in {"done", "none"}:
                continue
            try:
                people = extract_people(db, item)
                insight.people = people
                insight.people_status = "done" if people else "none"
                profiled += bool(people)
                db.commit()
            except Exception as exc:
                db.rollback()
                log.warning("profile extraction failed for %s: %s", item.id, exc)
    return {"status": "success", "examined": len(rows), "classified": classified, "profiled": profiled,
            "method": "llm" if llm.is_configured() else "keyword", "web_search": search_configured()}
