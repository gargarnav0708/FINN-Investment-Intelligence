# FINN

FINN is an evidence-first investment research desk for an Indian-equity portfolio. Its mobile-friendly web interface brings end-of-day market data and exchange announcements into a daily view, then links company research to primary filings and management transcripts. It is an analysis tool, not investment advice.

## What it does

- **Today:** View the tracked portfolio, market summaries, recent NSE announcements, a daily digest, and configured alerts. Refresh starts background jobs to fetch available market, announcement, and Tata Power document data.
- **Research:** Search the company universe and inspect company overviews, financial history, primary documents, and management commitments with source-page links. Listed companies can also be searched and registered on demand through Yahoo Finance listings for NSE (`.NS`) and BSE (`.BO`).
- **Evidence review:** Index registered PDFs and extract financial and management-statement candidates. Extracted financial figures require analyst validation before they appear as verified research data.
- **Thesis Drift:** Compare validated financial observations and available primary disclosures over time; unavailable evidence is shown as a coverage limitation rather than an implied conclusion.
- **Research questions:** Answer supported questions from validated data and citations. Optional model-backed retrieval is disabled unless an OpenAI-compatible provider is configured.

The starter portfolio has 18 companies. Live market history comes from Yahoo Finance; NSE announcements and linked filing PDFs are fetched on demand for NSE listings. Yahoo financial statements are cached separately from filing observations and are labeled provider sourced, with consolidated or standalone basis shown as uncertain. Original investor-relations document discovery still has issuer-specific adapters for Tata Power, BEL, and Infosys. Data availability depends on the external providers. The app does not provide intraday prices, BSE announcements, or complete document coverage for every company.

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

Model features (results reading, guidance extraction, promised-vs-delivered, research Q&A) need `FINN_LLM_API_KEY` (a Google Gemini key by default; any OpenAI-compatible endpoint via `FINN_LLM_BASE_URL`). Model choice per tier: `FINN_LLM_FAST_MODEL`, `FINN_LLM_STRONG_MODEL`. Without a key, the app still runs and shows documents and numbers. Do not commit `.env` or API keys.

## Any-ticker research, results reading and promised vs delivered

These run for **any NSE ticker** (not only the demo set), from primary sources:

| Step | Source | Model | Guard rail |
|---|---|---|---|
| Documents | screener.in used only as a link directory → the company's own annual report + last 4 concall transcripts (BSE / IR site PDFs) | — | PDFs are hash-verified and page-indexed |
| Delivered numbers | screener.in standardised quarterly / annual tables (built from exchange filings) | — | Machine-read results filings take precedence |
| Results headline (Revenue, EBITDA, PBT, PAT; QoQ & YoY) | NSE results filing PDF (one filing prints current, previous and year-ago quarter columns) | fast (Flash-Lite), page image + text | Every number must appear on the cited page; PBT − tax ≈ PAT in every column; column dates 3 and 12 months apart. Pass → auto-verified. Fail → shown as **unverified**, never hidden. |
| Guidance | Last 4 concall transcripts, full text with page markers | fast, one call per transcript | Quotes must be verbatim on the cited page (or are re-paged / dropped). Speakers are identified from the transcript; nothing is hard-coded per company. |
| Promised vs delivered | Grounded guidance + delivered table only | strong (Flash), one call per company | Every number the model cites is checked against the table; mismatches downgrade the verdict to *unverified*. |

**Free model setup (default recommendation):** the fast tier runs on Google Gemini Flash-Lite (long documents, large free quota); the strong tier runs on Groq `gpt-oss-120b` (free, fast, but ~8k tokens/minute, so FINN sizes the assessment and bull/bear prompts to fit and falls back to Gemini if Groq is busy). Each tier can point at any OpenAI-compatible provider via `FINN_LLM_<TIER>_BASE_URL / _API_KEY / _MODEL`.

Model calls go through `backend/app/llm.py`: two tiers, client-side rate pacing, retry on 429, automatic fallback from the strong to the fast tier when the free quota runs out, and a persistent cache keyed by model + prompt version + input hash (a filing or transcript is read once, ever). `GET /api/llm/usage` (or `python -m backend.app.cli llm-usage`) shows calls, cache hits and tokens by purpose.

In the app: **Research → pick or search any company → Commitments → "Read last 4 concalls + results"**. The job runs in the background (`POST /api/research/{ticker}/deep`, poll `GET /api/research-jobs/{id}`). Results filings in the portfolio digest are machine-read during **Refresh**.

CLI equivalents:

```sh
python -m backend.app.cli deep-research DIXON TITAN       # any NSE tickers
python -m backend.app.cli process-results --days 10       # read recent results filings across the portfolio
```

## Push intelligence, research views and copilot

- **Taxonomy + one-line summaries.** Every portfolio filing is mapped to the reference document's 17 categories by the fast model in batches of 20 (keyword rules as the fallback). Only high/medium items get a one-line summary; routine items are shown as the filing itself. `python -m backend.app.cli enrich-announcements --days 30`
- **Management changes.** The appointment/resignation PDF is read for each person's brief profile (required under SEBI LODR Reg. 30); the profile is shown only if it is found near-verbatim in the filing. With `TAVILY_API_KEY` or `SERPER_API_KEY` set, a short professional background is added from search results that name the person, with links.
- **Business snapshot.** What the company does and its segment revenue mix, read from the annual report's business/segment pages. Segment numbers are shown only if they appear on the cited page.
- **Bull vs bear.** Three points each way from the annual report and last four transcripts (reasoning model); each point carries a verbatim quote and page, and ungrounded points are dropped.
- **Today feed.** Phone-first: swipeable "what matters" cards with results numbers and people profiles inline; routine filings collapsed below.
- **Copilot.** "Ask FINN" works from any screen: questions naming a company (or asked on its page) are answered from its documents; others from the last 14 days of material portfolio filings and alerts. Citations are checked server-side.
- **End-of-day job.** Weekdays at 16:45 IST the app refreshes prices, filings, results, classifications and alerts (`FINN_EOD_TIME`, `FINN_SCHEDULER=off` to disable). On Render's free plan the instance sleeps, so `.github/workflows/eod-refresh.yml` calls `POST /api/refresh` at the same time: set the repository variable `FINN_URL`. The bell shows new alerts; allow browser notifications to get them while FINN is open.

## Deploy (Render)

NSE often blocks cloud IP ranges, so the deployment ships a pre-built database and works even if live scraping fails from the host.

1. Put your Gemini key in `.env` (`FINN_LLM_API_KEY=...`; free key from Google AI Studio).
2. Build the demo database on your own machine (Indian IP, so NSE works), then snapshot it:
   ```sh
   python -m backend.app.cli bootstrap-demo --research TATAPOWER INFY TITAN ASIANPAINT DIXON
   python -m backend.app.cli snapshot          # writes snapshot/finn.db.gz (commit this file)
   ```
3. Commit `snapshot/finn.db.gz` and push.
4. On render.com: **New → Blueprint →** select the repo (uses `render.yaml` + `Dockerfile`). In the service's **Environment** tab set `FINN_LLM_API_KEY`.
5. On first boot the app restores `snapshot/finn.db.gz` into `data/finn.db`. Source links whose PDF is not in the image redirect to the original public filing.

The free Render plan sleeps after inactivity and has an ephemeral disk: anything fetched on the server after boot is lost on restart, and the app falls back to the snapshot.

## Development

Run the test suite (tests use offline fixtures):

```sh
python -m pytest -q backend/tests
```

The FastAPI service exposes health and data-status endpoints, company and portfolio data, market history, announcements, documents, financial research, commitments, alerts, and thesis-drift analysis. See the interactive `/docs` page for the full, current API schema.
