from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AlertEvent, AlertPreference, Announcement, Company, DailyMarketData, PortfolioHolding, utcnow

IST = ZoneInfo("Asia/Kolkata")
ALERT_CATEGORIES = {
    "results": "Quarterly and annual results",
    "orders": "Material orders and contracts",
    "ma_restructuring": "Acquisitions, divestments and restructuring",
    "management": "Management changes",
    "capital": "Fundraising and capital events",
    "governance": "Regulatory, legal, governance and auditor events",
}


def classify_announcement(item: Announcement) -> dict:
    subject = item.subject or ""
    source_category = item.original_category or ""
    text = f"{source_category} {subject} {item.details or ''}".lower()
    category = "other"
    level = "routine"
    reason = "Routine exchange disclosure; retained in the digest for completeness."
    candidates = [
        ("results", "high", ("financial results", "quarterly results", "audited results", "unaudited results", "outcome of board meeting"), "Quarterly or annual results may change the reported financial picture."),
        ("ma_restructuring", "high", ("acquisition", "acquires", "divestment", "merger", "amalgamation", "restructuring", "scheme of arrangement"), "A transaction or restructuring may materially change the business or its capital structure."),
        ("orders", "high", ("bagging/receiving of orders", "order received", "letter of award", "contract awarded", "material order", "work order"), "A disclosed order or contract may affect the company’s revenue pipeline or execution workload."),
        ("capital", "high", ("fund raising", "fundraising", "qualified institutions placement", "preferential issue", "rights issue", "debt raise", "non-convertible debenture", "buyback", "dividend"), "A financing or capital-allocation decision can affect funding, dilution or shareholder returns."),
        ("management", "high", ("appointment of director", "resignation of director", "cessation of director", "chief financial officer", "managing director", "key managerial personnel", "auditor resignation"), "A senior leadership or auditor change may affect oversight or execution."),
        ("governance", "high", ("default", "insolvency", "fraud", "search and seizure", "regulatory action", "show cause notice", "penalty", "forensic audit", "auditor resignation", "material litigation"), "A governance, legal or regulatory event may create material business or control risk."),
        ("operations", "medium", ("commissioning", "commercial operation", "capacity addition", "project update", "production commenced", "plant shutdown", "project completion"), "An operating or project milestone may affect capacity or delivery."),
        ("corporate_action", "medium", ("postal ballot", "special resolution", "record date", "rights entitlement"), "A corporate action may require investor attention."),
    ]
    for bucket, priority, terms, explanation in candidates:
        if any(term in text for term in terms):
            category, level, reason = bucket, priority, explanation
            break
    if category == "other" and item.normalized_category in {"Investor meeting", "Shareholding", "Regulatory/compliance"}:
        category, level = "routine", "routine"
        reason = "Routine investor-relations or compliance disclosure; available for review but not elevated."
    return {"category": category, "materiality": level, "materiality_reason": reason}


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
