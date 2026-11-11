from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (CommitmentAssessment, Company, DocumentPage, FinancialObservation,
                     ManagementStatement, SourceDocument)

PARSER_VERSION = "management-turn-retrieval-v1"
MANAGERS = {"praveer sinha", "dr. praveer sinha", "sanjeev churiwala", "management"}
SPEAKER_RE = re.compile(r"(?m)^\s{0,35}([A-Z][A-Za-z.'’& -]{1,70}):\s*")
SIGNAL_RE = re.compile(r"\b(expect|will|target|plan|planned|guidance|commit(?:ted|ment)?|aim|forecast|by 20\d\d|during (?:the )?(?:year|quarter)|commission(?:ed|ing)?|operational by|capacity addition|growth of|around \d)\b", re.I)
NUM_RE = re.compile(r"(?<![\w.])(\d[\d,.]*(?:\.\d+)?)\s*(%|gigawatt|gw|megawatt|mw|x\b|crore)?", re.I)


def _norm(s: str) -> str:
    return " ".join(s.split())


def _date_from_text(text: str, doc: SourceDocument) -> date | None:
    if doc.document_date:
        return doc.document_date
    match = re.search(r"\b([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})\b", text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%B %d, %Y").date()
    except ValueError:
        return None


def _metric_topic(turn: str) -> tuple[str, str | None, str | None]:
    t = turn.lower()
    if any(k in t for k in ("megawatt", "gigawatt", "mw", "gw")):
        return "Capacity / project delivery", "capacity", "Installed or commissioned capacity"
    if "net debt" in t and "ebitda" in t:
        return "Leverage", "net_debt_to_ebitda", "Net debt divided by underlying EBITDA"
    if "revenue" in t:
        return "Revenue", "revenue", "Revenue from operations"
    if "ebitda" in t:
        return "EBITDA", "ebitda", "Reported EBITDA"
    if "pat" in t or "profit" in t:
        return "Profitability", "pat", "Reported profit after tax"
    if "margin" in t:
        return "Margin", "margin", "Profit or operating margin"
    if "capex" in t or "investment" in t:
        return "Capital expenditure", "capex", "Capital expenditure or investment"
    if "operational" in t or "commission" in t:
        return "Project milestone", "operational_milestone", "Project commissioning / operational date"
    return "Strategic outlook", None, None


def _designation(speaker: str) -> str:
    name = speaker.lower()
    if "praveer" in name:
        return "CEO & Managing Director"
    if "sanjeev" in name:
        return "Chief Financial Officer"
    return "Management (speaker not individually identified)"


def _classify(turn: str) -> tuple[str, str, float | None, float | None, str | None, str | None]:
    turn = _norm(turn)
    low = turn.lower()
    topic, metric, _ = _metric_topic(turn)
    target_low = target_high = None
    unit = None
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", turn) if part.strip()]
    capacity_pattern = r"\b(?:expect(?: that)?|target(?: this year)?|plan is|will (?:set up|add|commission|do|install|be|reach)|operational by|commission(?:ed|ing) by)\b[^.]{0,110}?\b(\d[\d,]*(?:\.\d+)?)\s*(gigawatt|gw|megawatt|mw)\b"
    percent_pattern = r"\b(?:target(?:ed)?|expect(?: that|ed)?|guidance|guide|grow(?:th)? of|margin of|up to)\b[^.]{0,90}?\b(\d[\d,]*(?:\.\d+)?)\s*%"
    financial_pattern = r"\b(?:revenue|ebitda|pat|profit|capex|investment)\b[^.]{0,90}?\b(?:target|expect|plan|will be|at)\b[^.]{0,50}?\b(?:Rs\.?|INR)?\s*(\d[\d,]*(?:\.\d+)?)\s*(crore|cr)\b"
    target_sentence = next((part for part in sentences if re.search(capacity_pattern, part, re.I) or re.search(percent_pattern, part, re.I) or re.search(financial_pattern, part, re.I)), None)
    target_text = target_sentence or turn
    capacity = re.search(capacity_pattern, target_text, re.I)
    percent = re.search(percent_pattern, target_text, re.I)
    financial = re.search(financial_pattern, target_text, re.I)
    if re.search(r"\b(withdraw|withdrawn|no longer|won't pursue|will not proceed)\b", low):
        cls = "withdrawal"
    elif re.search(r"\b(revise|revised|revising|instead of|now target|changed our)\b", low):
        cls = "revision"
    elif re.search(r"\b(reaffirm|remain on track|still on track|as mentioned earlier)\b", low):
        cls = "reaffirmation"
    elif re.search(r"\b(if|subject to|once .* approval|provided that|provided we|depending on)\b", low) and SIGNAL_RE.search(turn):
        cls = "conditional_guidance"
    elif (capacity or percent or financial) and re.search(r"\b(will|expect|target|plan|guidance|aim|by 20\d\d|this year|next year)\b", low):
        cls = "quantitative_guidance"
    elif re.search(r"\b(will|expect|plan|planned|committed|commitment|operational by|commission)\b", low) and re.search(r"\b(project|capacity|commission|operational|start work|complete|deliver|construct|investment|target)\b", low):
        cls = "operational_commitment"
    elif re.search(r"\b(expect|will|plan|aim|forecast|look forward|going forward|in future)\b", low):
        cls = "qualitative_forward_looking"
    else:
        cls = "non_commitment"
    target_period = None
    period_scope = target_text
    target_match = capacity or percent or financial
    if target_match:
        period_scope = target_text[max(0, target_match.start() - 48):target_match.end() + 48]
    period = re.search(r"\b(FY\s?\d{2,4}(?:\s?[-–]\s?\d{2,4})?|calendar year 20\d{2}|by 20\d{2}|in 20\d{2}|this year|next year|next \d+ (?:months?|quarters?))\b", period_scope, re.I)
    if period:
        target_period = period.group(0)
    if cls in {"quantitative_guidance", "conditional_guidance"}:
        match = capacity or percent or financial
        if match:
            target_low = float(match.group(1).replace(",", ""))
            target_high = target_low
            unit = match.group(2).lower() if capacity else "%" if percent else "crore"
            rng = re.search(rf"{re.escape(match.group(1))}\s*(?:{re.escape(unit)}\s*)?(?:to|[-–])\s*(\d[\d,]*(?:\.\d+)?)\s*{re.escape(unit)}", target_text, re.I)
            if rng:
                target_high = float(rng.group(1).replace(",", ""))
    return cls, topic, target_low, target_high, unit, target_period


def _turns(page_text: str, carried_speaker: str | None = None):
    matches = list(SPEAKER_RE.finditer(page_text))
    if matches and carried_speaker and matches[0].start() > 0:
        prefix = page_text[:matches[0].start()].strip()
        if len(prefix) >= 45:
            yield carried_speaker, prefix
    elif not matches and carried_speaker and len(page_text.strip()) >= 45:
        yield carried_speaker, page_text.strip()
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(page_text)
        speaker = _norm(match.group(1)).strip()
        body = page_text[start:end].strip()
        if len(body) >= 45:
            yield speaker, body


def _focus_quotes(body: str) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", body) if len(part.strip()) >= 30]
    forward = re.compile(r"\b(expect|will|target|plan|planned|commit(?:ted|ment)?|aim|forecast|operational by|commission(?:ed|ing) by|remain on track|reaffirm)\b", re.I)
    focused = [sentence for sentence in sentences if forward.search(sentence) and re.search(r"\d|project|capacity|revenue|ebitda|profit|margin|operational|commission|growth|capex", sentence, re.I)]
    if not focused:
        focused = [sentence for sentence in sentences if forward.search(sentence)]
    return focused[:6] or [body.strip()]


def extract_candidates(db: Session, ticker: str = "TATAPOWER") -> dict:
    company = db.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if company is None:
        return {"status": "failed", "error": f"Unknown company {ticker}"}
    docs = db.scalars(select(SourceDocument).where(SourceDocument.company_id == company.id, SourceDocument.document_type == "earnings_call_transcript").order_by(SourceDocument.id)).all()
    added = updated = 0
    source_pages = candidate_turns = 0
    for doc in docs:
        pages = db.scalars(select(DocumentPage).where(DocumentPage.document_id == doc.id).order_by(DocumentPage.page_number)).all()
        doc_date = _date_from_text(" ".join(p.extracted_text[:500] for p in pages[:2]), doc)
        carried_speaker = None
        for page in pages:
            source_pages += 1
            speakers = list(SPEAKER_RE.finditer(page.extracted_text))
            turns = list(_turns(page.extracted_text, carried_speaker))
            if speakers:
                carried_speaker = _norm(speakers[-1].group(1)).strip()
            for speaker, body in turns:
                if speaker.lower() not in MANAGERS:
                    continue
                if not SIGNAL_RE.search(body):
                    continue
                candidate_turns += 1
                for quote in _focus_quotes(body):
                    cls, topic, target_low, target_high, unit, target_period = _classify(quote)
                    if quote not in page.extracted_text:
                        continue
                    key = hashlib.sha256(f"{doc.id}|{page.page_number}|{speaker.lower()}|{_norm(quote)}".encode()).hexdigest()
                    row = db.scalar(select(ManagementStatement).where(ManagementStatement.evidence_key == key))
                    if row is None:
                        topic, metric, definition = _metric_topic(quote)
                        row = ManagementStatement(evidence_key=key, company_id=company.id, document_id=doc.id,
                            document_type=doc.document_type, transcript_date=doc_date, speaker=speaker,
                            designation=_designation(speaker),
                            page_number=page.page_number, exact_quote=quote, surrounding_context=body,
                            statement_class=cls, lifecycle=cls if cls in {"reaffirmation", "revision", "withdrawal"} else "original", topic=topic, metric_name=metric,
                            metric_definition=definition, target_low=target_low, target_high=target_high,
                            currency="INR" if unit == "crore" else None, unit=unit, target_period=target_period,
                            assumptions=None, extraction_status="candidate", review_status="needs_review",
                            parser_version=PARSER_VERSION)
                        db.add(row); added += 1
                    else:
                        # Re-extraction only refreshes classifier fields. It never overwrites review decisions.
                        row.statement_class=cls; row.lifecycle=cls if cls in {"reaffirmation", "revision", "withdrawal"} else row.lifecycle; row.designation=_designation(speaker); row.topic=topic; row.target_low=target_low; row.target_high=target_high
                        row.target_period=target_period; updated += 1
    db.commit()
    total = db.scalar(select(func.count(ManagementStatement.id)).where(ManagementStatement.company_id == company.id)) or 0
    return {"status": "success", "ticker": company.ticker, "documents_processed": len(docs), "pages_inspected": source_pages, "candidate_turns": candidate_turns, "inserted": added, "refreshed": updated, "total_candidates": total, "parser_version": PARSER_VERSION}


def _assessment_for(db: Session, statement: ManagementStatement) -> CommitmentAssessment:
    if statement.review_status != "approved":
        return CommitmentAssessment(statement_id=statement.id, status="needs_review", explanation="This candidate has not been approved by an analyst.")
    if statement.target_period and statement.transcript_date:
        period = statement.target_period.strip().lower()
        target_year = None
        year_match = re.search(r"(?:calendar year\s+|by\s+|in\s+)(20\d{2})", period)
        if year_match:
            target_year = int(year_match.group(1))
        elif period in {"this year", "this financial year"}:
            target_year = statement.transcript_date.year + (1 if statement.transcript_date.month >= 4 else 0)
        elif period in {"next year", "next financial year"}:
            target_year = statement.transcript_date.year + (2 if statement.transcript_date.month >= 4 else 1)
        if target_year:
            target_end = date(target_year, 3, 31) if period in {"this year", "this financial year", "next year", "next financial year"} else date(target_year, 12, 31)
            if date.today() < target_end:
                return CommitmentAssessment(statement_id=statement.id, status="in_progress", explanation=f"The target period ({statement.target_period}) has not ended yet. FINN will assess delivery only after that period and a comparable source-backed outcome are available.")
    if statement.target_period:
        period = statement.target_period.upper().replace(" ", "")
        fy = re.fullmatch(r"FY(20\d{2}|\d{2})(?:[-–](20\d{2}|\d{2}))?", period)
        if fy:
            end_token = fy.group(2) or fy.group(1)
            end_year = int(end_token) if len(end_token) == 4 else 2000 + int(end_token)
            if date.today() < date(end_year, 3, 31):
                return CommitmentAssessment(statement_id=statement.id, status="in_progress", explanation=f"The target period is {statement.target_period} and has not ended. FINN will assess delivery only after comparable full-period reported outcomes are available.")
            return CommitmentAssessment(statement_id=statement.id, status="awaiting_outcome", explanation=f"The target period ({statement.target_period}) has ended, but the indexed data contains quarterly rather than comparable full-year outcomes for this target.")
    if statement.metric_name in {"revenue", "pbt", "pat"} and statement.target_low is not None:
        rows = db.scalars(select(FinancialObservation).where(FinancialObservation.company_id == statement.company_id,
            FinancialObservation.canonical_metric == statement.metric_name,
            FinancialObservation.extraction_status == "validated").order_by(FinancialObservation.period_end.desc())).all()
        compatible = [r for r in rows if r.statement_basis == "consolidated" and r.currency == (statement.currency or "INR")]
        fy_match = re.search(r"FY\s*(20\d{2}|\d{2})\s*[-–]?\s*(20\d{2}|\d{2})?", statement.target_period or "", re.I)
        if compatible and fy_match and (statement.unit or "").lower() in {"crore", "cr"}:
            token = fy_match.group(2) or fy_match.group(1)
            end_year = int(token) if len(token) == 4 else 2000 + int(token)
            target_end = date(end_year, 3, 31)
            actual = next((r for r in compatible if r.period_end == target_end and r.period_type == "annual"), None)
            if actual is None:
                return CommitmentAssessment(statement_id=statement.id, status="awaiting_outcome", explanation=f"No validated full-year {statement.metric_name.upper()} observation is available for {statement.target_period}.")
            value = float(actual.normalised_value_in_crore)
            low, high = float(statement.target_low), float(statement.target_high if statement.target_high is not None else statement.target_low)
            if value < low: status = "missed"
            elif value > high: status = "exceeded"
            elif low == high: status = "achieved"
            else: status = "achieved"
            return CommitmentAssessment(statement_id=statement.id, status=status,
                explanation=f"Validated consolidated FY actual of INR {value:,.2f} crore compared with stated target {low:,.2f}–{high:,.2f} {statement.unit}.",
                formula=f"Actual {statement.metric_name.upper()} ({value:,.2f} crore) compared with target range [{low:,.2f}, {high:,.2f}] crore.",
                actual_value=value, actual_unit="INR crore", financial_observation_id=actual.id)
        if not compatible:
            return CommitmentAssessment(statement_id=statement.id, status="awaiting_outcome", explanation="No validated consolidated observation matches this guidance metric and target period.")
    return CommitmentAssessment(statement_id=statement.id, status="not_comparable", explanation="The indexed financial observations do not measure this operational or qualitative target on a comparable basis. FINN will not infer delivery without a matching source-backed metric.")


def assess_commitments(db: Session, ticker: str = "TATAPOWER") -> dict:
    company = db.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if company is None:
        return {"status": "failed", "error": f"Unknown company {ticker}"}
    rows = db.scalars(select(ManagementStatement).where(ManagementStatement.company_id == company.id, ManagementStatement.review_status == "approved")).all()
    for statement in rows:
        existing = db.scalar(select(CommitmentAssessment).where(CommitmentAssessment.statement_id == statement.id))
        assessment = _assessment_for(db, statement)
        if existing:
            for field in ("status", "explanation", "formula", "actual_value", "actual_unit", "financial_observation_id"):
                setattr(existing, field, getattr(assessment, field))
            existing.assessed_at = datetime.now(timezone.utc)
        else:
            db.add(assessment)
    db.commit()
    return {"status": "success", "assessed": len(rows), "ticker": company.ticker}


def statement_payload(db: Session, item: ManagementStatement) -> dict:
    assessment = db.scalar(select(CommitmentAssessment).where(CommitmentAssessment.statement_id == item.id))
    financial_metrics = {"revenue", "pbt", "pat", "ebitda", "margin", "capex", "net_debt_to_ebitda"}
    operational_metrics = {"capacity", "operational_milestone"}
    if item.metric_name in financial_metrics:
        triage_category = "financial"
    elif item.metric_name in operational_metrics or "capacity" in item.topic.lower() or "project" in item.topic.lower():
        triage_category = "operational"
    else:
        triage_category = "qualitative"
    if triage_category == "financial" and item.target_low is not None and item.target_period:
        triage_priority = "high"
    elif triage_category == "financial":
        triage_priority = "medium"
    elif triage_category == "operational" and item.target_low is not None:
        triage_priority = "medium"
    else:
        triage_priority = "low"
    return {"id": item.id, "ticker": item.company.ticker, "company": item.company.name, "document_id": item.document_id,
        "document_title": item.document.title, "document_type": item.document_type, "transcript_date": item.transcript_date.isoformat() if item.transcript_date else None,
        "speaker": item.speaker, "designation": item.designation, "page_number": item.page_number,
        "exact_quote": item.exact_quote, "surrounding_context": item.surrounding_context,
        "statement_class": item.statement_class, "lifecycle": item.lifecycle, "related_statement_id": item.related_statement_id, "topic": item.topic,
        "triage_category": triage_category, "review_priority": triage_priority,
        "metric_name": item.metric_name, "metric_definition": item.metric_definition,
        "target_low": item.target_low, "target_high": item.target_high, "currency": item.currency, "unit": item.unit,
        "target_period": item.target_period, "baseline_period": item.baseline_period, "assumptions": item.assumptions,
        "extraction_status": item.extraction_status, "review_status": item.review_status,
        "reviewer": item.reviewer, "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
        "review_note": item.review_note, "source_url": f"/api/documents/{item.document_id}/file#page={item.page_number}",
        "assessment": None if assessment is None else {"status": assessment.status, "explanation": assessment.explanation,
            "formula": assessment.formula, "actual_value": assessment.actual_value, "actual_unit": assessment.actual_unit,
            "financial_observation_id": assessment.financial_observation_id}}


def list_statements(db: Session, ticker: str, approved_only: bool = False) -> list[dict]:
    query = select(ManagementStatement).join(Company).where(Company.ticker == ticker.upper()).order_by(ManagementStatement.transcript_date, ManagementStatement.document_id, ManagementStatement.page_number, ManagementStatement.id)
    if approved_only:
        query = query.where(ManagementStatement.review_status == "approved")
    return [statement_payload(db, item) for item in db.scalars(query).all()]

