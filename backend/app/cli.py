from __future__ import annotations

import argparse
import json
import sys
from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from .database import Base, SessionLocal, engine, upgrade_schema
from .models import Company, FinancialObservation
from .seed import seed_companies
from .research import parse_documents, report_extraction, calculate_comparison
from sqlalchemy import func
from .services import discover_documents, research_company, sync_all, sync_announcements, sync_market
from .research import parse_documents
from .intelligence import evaluate_alerts
from .management import extract_candidates, assess_commitments
from .models import ManagementStatement


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finn", description="FINN data ingestion and database commands")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Create local SQLite tables")
    commands.add_parser("seed-companies", help="Upsert the 18-company demonstration portfolio")
    market = commands.add_parser("sync-market", help="Fetch six months of daily EOD data from Yahoo Finance")
    market.add_argument("--months", type=int, default=6, choices=range(1, 61))
    ann = commands.add_parser("sync-announcements", help="Fetch recent public NSE corporate announcements")
    ann.add_argument("--days", type=int, default=30, choices=range(1, 3661))
    docs = commands.add_parser("discover-documents", help="Discover official Tata Power source documents")
    docs.add_argument("--ticker", default="TATAPOWER")
    docs.add_argument("--no-download", action="store_true", help="Store metadata and links only")
    all_cmd = commands.add_parser("sync-all", help="Run market, announcement and Tata Power document jobs")
    all_cmd.add_argument("--days", type=int, default=30, choices=range(1, 3661))
    all_cmd.add_argument("--months", type=int, default=6, choices=range(1, 61))
    all_cmd.add_argument("--no-download", action="store_true")
    parse_cmd = commands.add_parser("parse-documents", help="Extract page text, tables and source-backed metric candidates")
    parse_cmd.add_argument("--ticker")
    parse_cmd.add_argument("--force", action="store_true")
    extract_cmd = commands.add_parser("extract-financials", help="Run deterministic headline financial extraction")
    extract_cmd.add_argument("--ticker")
    extract_cmd.add_argument("--force", action="store_true")
    calc_cmd = commands.add_parser("calculate-financials", help="Print comparable reported-period changes")
    calc_cmd.add_argument("--ticker", default="TATAPOWER")
    report_cmd = commands.add_parser("extraction-report", help="Print PDF, page, metric and review counts")
    report_cmd.add_argument("--ticker")
    full_cmd = commands.add_parser("process-research-documents", help="Parse the registered document set and print its quality report")
    full_cmd.add_argument("--ticker", default="TATAPOWER")
    full_cmd.add_argument("--force", action="store_true")
    review_cmd = commands.add_parser("review-financial-observations", help="Record a human review decision for observation IDs")
    review_selectors = review_cmd.add_mutually_exclusive_group(required=True)
    review_selectors.add_argument("--ids", help="Comma-separated financial observation IDs")
    review_selectors.add_argument("--document-ids", help="Comma-separated registered source-document IDs")
    review_cmd.add_argument("--metrics", default="revenue,pbt,pat", help="Comma-separated metrics used with --document-ids")
    review_cmd.add_argument("--status", choices=["validated", "needs_review", "rejected"], required=True)
    review_cmd.add_argument("--reviewer", required=True)
    review_cmd.add_argument("--note", required=True)
    research_cmd = commands.add_parser("research-company", help="Add an NSE ticker to the separate research universe")
    research_cmd.add_argument("--ticker", required=True)
    research_cmd.add_argument("--months", type=int, default=6, choices=range(1, 61))
    research_cmd.add_argument("--days", type=int, default=30, choices=range(1, 3661))
    commands.add_parser("evaluate-alerts", help="Evaluate configured portfolio announcement and EOD-market alert rules")
    bootstrap_cmd = commands.add_parser("bootstrap", help="Initialize, seed and ingest the first available portfolio and Tata Power sources")
    bootstrap_cmd.add_argument("--days", type=int, default=30, choices=range(1, 3661))
    bootstrap_cmd.add_argument("--months", type=int, default=6, choices=range(1, 61))
    mgmt_extract = commands.add_parser("extract-management-guidance", help="Retrieve source-linked management statement candidates from indexed transcripts")
    mgmt_extract.add_argument("--ticker", default="TATAPOWER")
    mgmt_report = commands.add_parser("management-extraction-report", help="Report transcript candidate, review and outcome counts")
    mgmt_report.add_argument("--ticker", default="TATAPOWER")
    mgmt_review = commands.add_parser("review-management-statement", help="Approve, reject or return a statement candidate to review")
    mgmt_review.add_argument("--id", type=int, required=True)
    mgmt_review.add_argument("--reviewer", required=True)
    mgmt_review.add_argument("--action", choices=["approved", "rejected", "needs_review"], required=True)
    mgmt_review.add_argument("--note", default="")
    mgmt_assess = commands.add_parser("assess-commitments", help="Assess approved guidance against comparable validated outcomes")
    mgmt_assess.add_argument("--ticker", default="TATAPOWER")
    args = parser.parse_args(argv)

    Base.metadata.create_all(bind=engine)
    upgrade_schema(engine)
    with SessionLocal() as db:
        if args.command == "init-db":
            print(json.dumps({"status": "ok", "database": str(engine.url)}))
            return 0
        if args.command == "seed-companies":
            print(json.dumps(seed_companies(db), indent=2))
            return 0
        if args.command == "sync-market":
            result = sync_market(db, args.months)
        elif args.command == "sync-announcements":
            result = sync_announcements(db, args.days)
        elif args.command == "discover-documents":
            result = discover_documents(db, args.ticker, not args.no_download)
        elif args.command == "sync-all":
            result = sync_all(db, args.days, not args.no_download, args.months)
        elif args.command == "research-company":
            result = research_company(db, args.ticker, args.months, args.days)
        elif args.command == "evaluate-alerts":
            result = evaluate_alerts(db)
        elif args.command == "extract-management-guidance":
            result = extract_candidates(db, args.ticker)
        elif args.command == "management-extraction-report":
            rows = db.scalars(select(ManagementStatement).join(Company).where(Company.ticker == args.ticker.upper())).all()
            result = {"ticker": args.ticker.upper(), "candidate_count": len(rows), "review_status": {s: sum(r.review_status == s for r in rows) for s in ["needs_review", "approved", "rejected"]}, "classes": {c: sum(r.statement_class == c for r in rows) for c in sorted({r.statement_class for r in rows})}, "documents": sorted({r.document.title for r in rows})}
        elif args.command == "review-management-statement":
            item = db.get(ManagementStatement, args.id)
            if item is None:
                parser.error(f"unknown management statement id: {args.id}")
            item.review_status = args.action; item.reviewer = args.reviewer; item.reviewed_at = datetime.now(timezone.utc); item.review_note = args.note or None
            db.commit()
            result = {"id": item.id, "ticker": item.company.ticker, "review_status": item.review_status, "reviewer": item.reviewer, "reviewed_at": item.reviewed_at.isoformat()}
        elif args.command == "assess-commitments":
            result = assess_commitments(db, args.ticker)
        elif args.command == "bootstrap":
            seeded = seed_companies(db)
            ingestion = sync_all(db, lookback_days=args.days, download_docs=True, market_months=args.months)
            parsed = parse_documents(db, ticker="TATAPOWER", force=False)
            result = {"status": "success" if not any(part.get("status") in {"failed", "partial", "partial_failure"} for part in [ingestion.get("market", {}), ingestion.get("announcements", {}), ingestion.get("documents", {}), parsed]) else "partial", "database": str(engine.url), "seed": seeded, "ingestion": ingestion, "document_processing": parsed}
        elif args.command in {"parse-documents", "extract-financials", "process-research-documents"}:
            result = parse_documents(db, args.ticker, args.force)
            if args.command in {"extract-financials", "process-research-documents"}:
                result["quality_report"] = report_extraction(db, args.ticker)
        elif args.command == "extraction-report":
            result = report_extraction(db, args.ticker)
        elif args.command == "calculate-financials":
            company = db.scalar(select(Company).where(func.upper(Company.ticker) == args.ticker.upper()))
            if company is None:
                parser.error(f"unknown ticker: {args.ticker}")
            result = {"ticker": company.ticker, "comparisons": []}
            rows = db.scalars(select(FinancialObservation).where(FinancialObservation.company_id == company.id).order_by(FinancialObservation.period_end, FinancialObservation.canonical_metric)).all()
            grouped: dict[tuple[str, str], list[FinancialObservation]] = {}
            for row in rows:
                grouped.setdefault((row.canonical_metric, row.statement_basis), []).append(row)
            for (metric, basis), items in grouped.items():
                periods = {item.period_end: item for item in items if item.extraction_status == "validated"}
                for current in periods.values():
                    previous_month = current.period_end.month - 3
                    previous_year = current.period_end.year
                    if previous_month <= 0:
                        previous_month += 12
                        previous_year -= 1
                    previous_end = date(previous_year, previous_month, monthrange(previous_year, previous_month)[1])
                    qoq = calculate_comparison(current, periods.get(previous_end), "qoq")
                    try:
                        yoy_end = current.period_end.replace(year=current.period_end.year - 1)
                    except ValueError:
                        yoy_end = current.period_end.replace(year=current.period_end.year - 1, day=28)
                    yoy = calculate_comparison(current, periods.get(yoy_end), "yoy")
                    result["comparisons"].append({"metric": metric, "basis": basis, "period_end": current.period_end.isoformat(), "qoq": qoq, "yoy": yoy})
        elif args.command == "review-financial-observations":
            if args.ids:
                ids = [int(value.strip()) for value in args.ids.split(",") if value.strip()]
                items = db.scalars(select(FinancialObservation).where(FinancialObservation.id.in_(ids))).all()
            else:
                document_ids = [int(value.strip()) for value in args.document_ids.split(",") if value.strip()]
                metric_names = [value.strip().lower() for value in args.metrics.split(",") if value.strip()]
                items = db.scalars(select(FinancialObservation).where(
                    FinancialObservation.document_id.in_(document_ids),
                    FinancialObservation.canonical_metric.in_(metric_names),
                )).all()
                ids = [item.id for item in items]
            conflicts: dict[tuple, set[Decimal]] = {}
            for item in items:
                key = (item.canonical_metric, item.statement_basis, item.period_end)
                conflicts.setdefault(key, set()).add(Decimal(item.normalised_value_in_crore))
            conflict_keys = {key for key, values in conflicts.items() if len(values) > 1}
            updated = 0
            for item in items:
                key = (item.canonical_metric, item.statement_basis, item.period_end)
                if args.status == "validated" and key in conflict_keys:
                    item.extraction_status = "needs_review"
                else:
                    item.extraction_status = args.status
                item.reviewer = args.reviewer
                item.reviewed_at = datetime.now(timezone.utc)
                item.review_note = args.note.strip()
                updated += 1
            db.commit()
            result = {"updated": updated, "requested": len(ids), "status": args.status, "conflict_groups": [{"metric": key[0], "basis": key[1], "period_end": key[2].isoformat()} for key in sorted(conflict_keys)], "reviewed_at": datetime.now(timezone.utc).isoformat()}
        else:
            parser.error("unknown command")
            return 2
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    statuses = [result.get("status")] if isinstance(result, dict) else []
    statuses += [v.get("status") for v in result.values() if isinstance(v, dict) and "status" in v]
    if "failed" in statuses:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
