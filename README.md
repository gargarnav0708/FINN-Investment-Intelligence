# FINN

FINN is an evidence-first investment research desk for an Indian-equity portfolio. Its mobile-friendly web interface brings end-of-day market data and exchange announcements into a daily view, then links company research to primary filings and management transcripts. It is an analysis tool, not investment advice.

## What it does

- **Today:** View the tracked portfolio, market summaries, recent NSE announcements, a daily digest, and configured alerts. Refresh starts background jobs to fetch available market, announcement, and Tata Power document data.
- **Research:** Search the company universe and inspect company overviews, financial history, primary documents, and management commitments with source-page links.
- **Evidence review:** Index registered PDFs and extract financial and management-statement candidates. Extracted financial figures require analyst validation before they appear as verified research data.
- **Thesis Drift:** Compare validated financial observations and available primary disclosures over time; unavailable evidence is shown as a coverage limitation rather than an implied conclusion.
- **Research questions:** Answer supported questions from validated data and citations. Optional model-backed retrieval is disabled unless an OpenAI-compatible provider is configured.

The starter portfolio has 18 companies. Live market history comes from Yahoo Finance, announcements from NSE, and primary-document discovery currently focuses on Tata Power. Data availability depends on the external providers. The app does not provide intraday prices, BSE announcements, or complete document coverage for every company.

## Requirements

- Python 3.10 or later
- Internet access for the initial live-data import and subsequent refreshes

## Install and run

From the project directory:

```sh
python3 -m venv .venv
```

Activate the virtual environment and install dependencies:

```sh
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
# .venv\Scripts\Activate.ps1

python -m pip install -r backend/requirements.txt
```

Start the app:

```sh
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8001
```

Open <http://127.0.0.1:8001>. The API and frontend are served from the same origin. Do not open `index.html` directly from the filesystem. Interactive API documentation is at <http://127.0.0.1:8001/docs>.

On first startup, FINN creates its SQLite schema and seeds the company list. To also fetch initial data and index available Tata Power documents, run this once before or after starting the server:

```sh
python -m backend.app.cli bootstrap --days 30 --months 6
```

Bootstrap and refresh require network access and can finish with partial results if an external provider is unavailable. The app can still start and display whatever data is available. The database is created at `data/finn.db`; downloaded documents and temporary provider responses are stored under `data/`.

## Using the app

1. Open **Today** to review the portfolio, market snapshot, announcements, and alerts.
2. Select **Live Data** to use persisted provider data, or choose **Demo Data** where available to explore illustrative interface content.
3. Use **Refresh** to request updated market data, NSE announcements, and configured Tata Power documents. The progress area reports background job status.
4. Open **Research**, search for a company, and explore its overview, financials, commitments, and documents.
5. Treat extracted figures and management statements as candidates until reviewed. Validated figures link back to the source document and page.

## Refreshing data from the command line

Run commands from the project directory with the virtual environment active:

```sh
# Refresh about six months of end-of-day prices
python -m backend.app.cli sync-market --months 6

# Fetch recent NSE corporate announcements
python -m backend.app.cli sync-announcements --days 30

# Discover and download supported official company documents
python -m backend.app.cli discover-documents --ticker TATAPOWER

# Run the available ingestion jobs together
python -m backend.app.cli sync-all --days 30
```

Process PDFs and inspect financial extraction candidates:

```sh
python -m backend.app.cli process-research-documents --ticker TATAPOWER
python -m backend.app.cli extraction-report --ticker TATAPOWER
```

Review individual candidates in the app or use the CLI review commands. Only mark figures validated after checking the exact source row, reporting period, and cited PDF page.

## Configuration

Copy `.env.example` to `.env` to customize local settings. By default, FINN uses SQLite at `data/finn.db`; set `DATABASE_URL` to use another SQLAlchemy-compatible database. CORS is disabled unless you set `FINN_CORS_ORIGINS` to the exact origins of a separately hosted frontend.

An optional OpenAI-compatible chat-completions provider can be configured with `FINN_LLM_BASE_URL`, `FINN_LLM_MODEL`, and `FINN_LLM_API_KEY`. Leave these unset to keep model-backed retrieval disabled. Do not commit `.env` or API keys.

## Development

Run the test suite (tests use offline fixtures):

```sh
python -m pytest -q backend/tests
```

The FastAPI service exposes health and data-status endpoints, company and portfolio data, market history, announcements, documents, financial research, commitments, alerts, and thesis-drift analysis. See the interactive `/docs` page for the full, current API schema.
