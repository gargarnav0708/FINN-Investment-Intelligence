from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Company, PortfolioHolding

PORTFOLIO = [
    ("Reliance Industries", "RELIANCE", "Energy & Retail"),
    ("Tata Consultancy Services", "TCS", "Information Technology"),
    ("HDFC Bank", "HDFCBANK", "Banking"),
    ("ICICI Bank", "ICICIBANK", "Banking"),
    ("Infosys", "INFY", "Information Technology"),
    ("Larsen & Toubro", "LT", "Engineering & Construction"),
    ("Bharti Airtel", "BHARTIARTL", "Telecommunications"),
    ("ITC", "ITC", "Consumer Goods"),
    ("Tata Motors Passenger Vehicles Limited", "TMPV", "Automobiles"),
    ("Tata Power Company", "TATAPOWER", "Utilities"),
    ("Sun Pharmaceutical Industries", "SUNPHARMA", "Pharmaceuticals"),
    ("Maruti Suzuki India", "MARUTI", "Automobiles"),
    ("NTPC", "NTPC", "Utilities"),
    ("Power Grid Corporation of India", "POWERGRID", "Utilities"),
    ("Adani Ports and Special Economic Zone", "ADANIPORTS", "Transportation"),
    ("Titan Company", "TITAN", "Consumer Goods"),
    ("Asian Paints", "ASIANPAINT", "Consumer Goods"),
    ("Axis Bank", "AXISBANK", "Banking"),
]


def seed_companies(db: Session) -> dict:
    inserted = 0
    updated = 0
    active_tickers = {ticker for _, ticker, _ in PORTFOLIO}
    for holding in db.scalars(select(PortfolioHolding)).all():
        if holding.company.ticker not in active_tickers:
            holding.is_active = False
    for name, ticker, sector in PORTFOLIO:
        company = db.scalar(select(Company).where(Company.ticker == ticker))
        if company is None and ticker == "TMPV":
            # NSE changed the old TATAMOTORS symbol to TMPV in October 2025.
            company = db.scalar(select(Company).where(Company.ticker == "TATAMOTORS"))
            if company is not None:
                company.ticker = "TMPV"
        if company is None:
            company = Company(name=name, ticker=ticker, exchange="NSE", sector=sector, yfinance_symbol=f"{ticker}.NS")
            db.add(company)
            db.flush()
            db.add(PortfolioHolding(company_id=company.id, is_active=True))
            inserted += 1
        else:
            company.name = name
            company.sector = sector
            company.exchange = "NSE"
            company.yfinance_symbol = f"{ticker}.NS"
            holding = db.scalar(select(PortfolioHolding).where(PortfolioHolding.company_id == company.id))
            if holding is None:
                db.add(PortfolioHolding(company_id=company.id, is_active=True))
            elif not holding.is_active:
                holding.is_active = True
            updated += 1
        if ticker == "TATAPOWER":
            company.website = "https://www.tatapower.com/"
            company.investor_relations_url = "https://www.tatapower.com/investor-resource-center/analyst-call-transcript-tab"
    benchmark = db.scalar(select(Company).where(Company.ticker == "NIFTY50"))
    if benchmark is None:
        db.add(Company(name="NIFTY 50", ticker="NIFTY50", exchange="NSE", sector="Index", yfinance_symbol="^NSEI"))
    else:
        benchmark.name = "NIFTY 50"
        benchmark.yfinance_symbol = "^NSEI"
    db.commit()
    return {"inserted": inserted, "updated": updated, "total": len(PORTFOLIO)}
