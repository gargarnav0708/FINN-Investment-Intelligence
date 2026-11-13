from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AlertEvent, AlertPreference, Announcement, Company, DailyMarketData, PortfolioHolding, utcnow

IST = ZoneInfo("Asia/Kolkata")
# Reference taxonomy (ENAM brief): key -> (label, default materiality, why it can matter)
TAXONOMY: dict[str, tuple[str, str, str]] = {
    "results": ("Results", "high", "Quarterly or annual results may change the reported financial picture."),
    "M&A": ("M&A", "high", "An acquisition, merger, demerger or JV can change the business or its capital structure."),
    "fund_raise": ("Fund raise", "high", "New equity or debt can mean dilution, leverage or a change in capital allocation."),
    "management_change": ("Management change", "high", "A change at the top can affect strategy, execution or oversight."),
    "litigation": ("Litigation / regulatory", "high", "A legal or regulatory action may create financial or governance risk."),
    "insider_trading": ("Insider trading", "medium", "Promoter or insider dealing in the stock can signal their view."),
    "earnings_call": ("Earnings call", "medium", "Concall material carries management's own guidance and tone."),
    "investor_meetings": ("Investor meetings", "routine", "Scheduled meetings with investors; content is rarely disclosed."),
    "credit_rating": ("Credit rating", "medium", "A rating action changes the cost and availability of debt."),
    "dividend": ("Dividend", "medium", "A cash payout to shareholders, with a record date."),
    "bonus_split": ("Bonus / split", "medium", "Share count changes; no cash changes hands."),
    "capex": ("Capex", "medium", "New capacity or a plant changes future growth and the funding need."),
    "press_release": ("Press release", "medium", "Order wins, launches or partnerships may affect the revenue pipeline."),
    "postal_ballot": ("Postal ballot", "routine", "Shareholder vote notice or result."),
    "agm_egm": ("AGM / EGM", "routine", "General meeting notice or outcome."),
    "pledging": ("Pledging", "high", "Promoter pledges are a leverage and governance risk signal."),
    "unclassified": ("Routine", "routine", "Routine compliance paperwork; kept for completeness, not elevated."),
}
MATERIAL_CATEGORIES = [k for k in TAXONOMY if k not in {"investor_meetings", "postal_ballot", "agm_egm", "unclassified", "earnings_call", "bonus_split"}]
ALERT_CATEGORIES = {key: TAXONOMY[key][0] for key in MATERIAL_CATEGORIES}

ROUTINE_TERMS = ("trading window", "loss of share certificate", "duplicate share", "74(5)", "compliance certificate",
                 "esop", "esos", "employee stock option", "brsr", "business responsibility", "certificate under regulation",
                 "reconciliation of share capital", "secretarial compliance", "shareholding pattern", "change in registrar",
                 "voting results of", "scrutinizer")
KEYWORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pledging", ("pledge", "encumbrance", "regulation 31")),
    ("insider_trading", ("insider trading", "regulation 7(2)", "regulation 7 (2)", "regulation 29", "substantial acquisition of shares", "sast")),
    ("earnings_call", ("transcript", "earnings call", "earning call", "conference call", "concall", "audio recording", "audio/video recording")),
    ("results", ("financial result", "quarterly results", "audited results", "unaudited", "integrated filing")),
    ("credit_rating", ("credit rating", "crisil", "icra", "care ratings", "india ratings", "rating action", "revision in rating", "rating")),
    ("fund_raise", ("qualified institutions placement", "qip", "preferential issue", "preferential allotment", "rights issue", "fund raising",
                    "fundraising", "non-convertible debenture", "ncd", "commercial paper", "buyback", "buy-back", "debenture", "bond issue", "raising of funds")),
    ("bonus_split", ("bonus issue", "bonus shares", "sub-division", "subdivision", "stock split", "split of")),
    ("dividend", ("dividend",)),
    ("management_change", ("appointment", "resignation", "cessation", "change in director", "key managerial personnel", "chief financial officer",
                           "chief executive", "managing director", "re-appointment", "retirement", "change in management", "senior management")),
    ("M&A", ("acquisition", "acquire", "merger", "amalgamation", "demerger", "scheme of arrangement", "joint venture", "divest", "disinvest",
             "incorporation of", "wholly owned subsidiary", "stake sale", "sale of stake", "investment in subsidiary", "slump sale")),
    ("litigation", ("penalty", "show cause", "court", "tribunal", "nclt", "nclat", "insolvency", "arbitration", "lawsuit", "fine imposed",
                    "warning letter", "search and seizure", "regulatory action", "adjudication", "demand order", "tax demand", "litigation", "fraud", "default")),
    ("capex", ("capex", "capital expenditure", "capacity expansion", "new plant", "commissioning", "commissioned", "greenfield", "brownfield", "commercial operation", "expansion project")),
    ("postal_ballot", ("postal ballot", "e-voting", "remote e-voting")),
    ("agm_egm", ("annual general meeting", " agm", "egm", "extra-ordinary general meeting", "extraordinary general meeting", "book closure")),
    ("investor_meetings", ("analyst", "investor meet", "institutional investor", "investor conference", "investors meet", "one-on-one")),
    ("press_release", ("press release", "media release", "order", "contract", "letter of award", "launch", "partnership", "clarification", "agreement", "mou")),
)


def keyword_category(item: Announcement) -> str:
    text = f" {item.original_category or ''} {item.subject or ''} {item.details or ''} ".lower()
    head = f" {item.original_category or ''} {item.subject or ''} ".lower()
    if "auditor" in text and "resign" in text:
        return "management_change"
    if any(term in head for term in ROUTINE_TERMS):
        return "unclassified"
    for category, terms in KEYWORD_RULES:
        if any(term in text for term in terms):
            if category == "management_change" and "auditor" in text:
                return "unclassified"  # auditor appointments are routine per the reference taxonomy
            return category
    return "unclassified"


def keyword_materiality(item: Announcement, category: str) -> str:
    text = f"{item.subject or ''} {item.details or ''}".lower()
    level = TAXONOMY[category][1]
    if category == "results" and any(t in text for t in ("newspaper", "publication", "intimation of board meeting", "prior intimation")):
        return "routine"
    if category == "capex" and any(t in text for t in ("approv", "board", "capex of", "investment of", "crore")):
        return "high"  # a decision to spend, not an operating update
    if category == "credit_rating" and any(t in text for t in ("downgrade", "negative", "watch with negative")):
        return "high"
    if category == "earnings_call" and any(t in text for t in ("intimation", "schedule")):
        return "routine"
    if category == "management_change" and any(t in text for t in ("company secretary", "compliance officer")) and not any(t in text for t in ("chief", "managing director", "director")):
        return "medium"
    return level


def classify_announcement(item: Announcement) -> dict:
    try:
        insight = item.insight
    except Exception:
        insight = None
    if insight is not None and insight.category in TAXONOMY:
        label, _, why = TAXONOMY[insight.category]
        return {"category": insight.category, "category_label": label, "materiality": insight.materiality,
                "materiality_reason": insight.reason or why, "summary": insight.summary, "classification_method": insight.method,
                "people": insight.people or []}
    category = keyword_category(item)
    label, _, why = TAXONOMY[category]
    return {"category": category, "category_label": label, "materiality": keyword_materiality(item, category),
            "materiality_reason": why, "summary": None, "classification_method": "keyword", "people": []}


def get_alert_preferences(db: Session, create: bool = True) -> AlertPreference | None:
    prefs = db.get(AlertPreference, 1)
    if prefs is None and create:
        prefs = AlertPreference(id=1, announcement_categories={key: True for key in ALERT_CATEGORIES})
        db.add(prefs)
        db.commit()
        db.refresh(prefs)
    return prefs


def alert_preferences_payload(prefs: AlertPreference) -> dict:
    configured = prefs.announcement_categories or {}
    return {
        "announcement_categories": {key: bool(configured.get(key, True)) for key in ALERT_CATEGORIES},
        "category_labels": ALERT_CATEGORIES,
        "positive_price_move_pct": prefs.positive_price_move_pct,
        "negative_price_move_pct": prefs.negative_price_move_pct,
        "unusual_volume_multiple": prefs.unusual_volume_multiple,
        "updated_at": prefs.updated_at.isoformat() if prefs.updated_at else None,
    }


def _event_key(rule_type: str, evidence_id: int, detail: str = "") -> str:
    return hashlib.sha256(f"{rule_type}|{evidence_id}|{detail}".encode()).hexdigest()


def evaluate_alerts(db: Session, now: datetime | None = None) -> dict:
    now = now or datetime.now(IST)
    prefs = get_alert_preferences(db)
    assert prefs is not None
    categories = prefs.announcement_categories or {}
    holdings = db.scalars(select(Company).join(PortfolioHolding).where(PortfolioHolding.is_active.is_(True))).all()
    created = 0
    examined_announcements = examined_market = 0
    events = []
    for company in holdings:
        announcements = db.scalars(select(Announcement).where(Announcement.company_id == company.id)).all()
        for item in announcements:
            if item.announcement_datetime is None:
                continue
            stamp = item.announcement_datetime
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=IST)
            else:
                stamp = stamp.astimezone(IST)
            # On first activation, evaluate only genuinely recent filings rather
            # than backfilling the entire historical announcement catalogue.
            if stamp < now.astimezone(IST) - timedelta(hours=24):
                continue
            assessment = classify_announcement(item)
            category = assessment["category"]
            if category not in ALERT_CATEGORIES or not categories.get(category, True):
                continue
            examined_announcements += 1
            key = _event_key("announcement", item.id, category)
            if db.scalar(select(AlertEvent.id).where(AlertEvent.dedupe_key == key)):
                continue
            event = AlertEvent(
                dedupe_key=key, company_id=company.id, announcement_id=item.id,
                rule_type="announcement", category=category, triggered_at=stamp,
                reason=f"{assessment['materiality'].title()} priority — {item.subject}. {assessment['materiality_reason']}",
                trigger_data={"materiality": assessment["materiality"], "subject": item.subject, "source": item.source},
            )
            db.add(event); events.append(event); created += 1

        latest_rows = db.scalars(select(DailyMarketData).where(DailyMarketData.company_id == company.id).order_by(DailyMarketData.trading_date.desc()).limit(25)).all()
        market = None
        for row in latest_rows:
            if row.trading_date == now.astimezone(IST).date() and now.astimezone(IST).time() < time(16, 0):
                continue
            market = row
            break
        if market is None:
            continue
        examined_market += 1
        pct = market.daily_return_pct
        if pct is not None and pct >= prefs.positive_price_move_pct:
            specs = [("price_up", f"Closing price rose {pct:.1f}% on {market.trading_date.isoformat()}; your threshold is +{prefs.positive_price_move_pct:.1f}%.", {"daily_return_pct": pct, "threshold_pct": prefs.positive_price_move_pct})]
        elif pct is not None and pct <= -prefs.negative_price_move_pct:
            specs = [("price_down", f"Closing price fell {abs(pct):.1f}% on {market.trading_date.isoformat()}; your threshold is −{prefs.negative_price_move_pct:.1f}%.", {"daily_return_pct": pct, "threshold_pct": prefs.negative_price_move_pct})]
        else:
            specs = []
        if market.relative_volume is not None and market.average_volume_20 is not None and market.relative_volume >= prefs.unusual_volume_multiple:
            specs.append(("unusual_volume", f"Trading volume reached {market.relative_volume:.1f}× its trailing 20-session average on {market.trading_date.isoformat()}; your threshold is {prefs.unusual_volume_multiple:.1f}×.", {"relative_volume": market.relative_volume, "volume": market.volume, "average_volume_20": market.average_volume_20, "threshold_multiple": prefs.unusual_volume_multiple}))
        for rule_type, reason, detail in specs:
            key = _event_key(rule_type, market.id)
            if db.scalar(select(AlertEvent.id).where(AlertEvent.dedupe_key == key)):
                continue
            event = AlertEvent(dedupe_key=key, company_id=company.id, market_data_id=market.id, rule_type=rule_type, triggered_at=datetime.combine(market.trading_date, time(16, 0), tzinfo=IST), reason=reason, trigger_data=detail)
            db.add(event); events.append(event); created += 1
    db.commit()
    return {"status": "success", "portfolio_companies": len(holdings), "announcements_examined": examined_announcements, "market_observations_examined": examined_market, "events_created": created, "events": [alert_event_payload(e) for e in events]}


def alert_event_payload(event: AlertEvent) -> dict:
    triggered = event.triggered_at
    if triggered.tzinfo is None:
        triggered = triggered.replace(tzinfo=IST)
    return {
        "id": event.id, "ticker": event.company.ticker, "company": event.company.name,
        "rule_type": event.rule_type, "category": event.category,
        "triggered_at": triggered.isoformat(), "reason": event.reason,
        "trigger_data": event.trigger_data,
        "announcement_id": event.announcement_id,
        "market_data_id": event.market_data_id,
        "source_url": event.announcement.attachment_url if event.announcement else None,
        "source_page_url": event.announcement.source_page_url if event.announcement else None,
        "market_date": event.market_data.trading_date.isoformat() if event.market_data else None,
        "market_return_pct": event.market_data.daily_return_pct if event.market_data else None,
        "relative_volume": event.market_data.relative_volume if event.market_data else None,
    }


def digest_bounds(window: str, now: datetime | None = None, from_date: date | None = None, to_date: date | None = None) -> tuple[datetime, datetime]:
    local_now = (now or datetime.now(IST)).astimezone(IST)
    if from_date or to_date:
        if not from_date or not to_date or from_date > to_date:
            raise ValueError("Provide both from_date and to_date, with from_date on or before to_date.")
        return datetime.combine(from_date, time.min, IST), datetime.combine(to_date + timedelta(days=1), time.min, IST)
    durations = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
    if window not in durations:
        raise ValueError("window must be 24h, 7d, or 30d")
    return local_now - durations[window], local_now
