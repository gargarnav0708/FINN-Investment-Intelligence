# FINN — writeup

**Prototype:** https://finn-vno3.onrender.com · **Repository:** https://github.com/gargarnav0708/FINN-Investment-Intelligence · **AI build history:** `docs/BUILD_HISTORY.md` (with conversation links)

## What I built

FINN is a mobile-first web app that does the two jobs in the brief.

**Push — "tell me what matters today."** For an 18-stock portfolio, FINN pulls end-of-day prices and NSE announcements, classifies every filing into the reference taxonomy and decides what is material. Material items lead the day as swipeable cards. A results filing shows Revenue, EBITDA, PBT and PAT with QoQ and YoY, read from the filing itself. A management change shows who the incoming person is, from the profile the filing must carry, plus an optional web background with links. Everything else material gets a one-line summary; routine paperwork is collapsed. Alerts cover price moves, volume spikes and categories, and an end-of-day job runs on weekdays.

**Pull — "brief me on a company,"** for any NSE ticker, including ones outside the portfolio. FINN finds the annual report and last four concall transcripts and returns: a business snapshot with segment revenue mix; a five-year view of growth, margins, debt and cash conversion; management's guidance; promised vs delivered across the last four calls with a credibility verdict; and a bull and bear case. An "Ask FINN" copilot answers questions from the stored filings on any screen.

## Key decisions

1. **Verify, don't trust.** Models are only allowed to produce things that can be checked mechanically. Results numbers must appear on the cited page and reconcile (PBT − tax = PAT across all three quarter columns). Guidance and bull/bear quotes must be found verbatim in the source, or they are dropped. Numbers a model cites in a promised-vs-delivered verdict must match the delivered-numbers table, or the verdict becomes "unverified". Failures are shown as unverified rather than hidden, and every figure links to its page.
2. **One filing is enough for QoQ and YoY.** Indian results filings print this quarter, last quarter and the same quarter last year side by side, so the headline numbers need no history database.
3. **Route by task, cache everything.** A cheap long-context model (Gemini Flash-Lite) does the bulk reading; a reasoning model (Groq gpt-oss-120b) does only the judgement steps, with prompts sized to its free-tier budget. Every call is cached by input hash, so a document is read once. The whole system runs on free tiers.
4. **Deterministic where possible.** Ratios, growth rates, margins and the delivered-numbers table are computed in code, not by a model.
5. **Built to demo reliably.** NSE often blocks cloud IPs, so the deployment ships a pre-built database and falls back to it; source links redirect to the original filing.

## Trade-offs

- screener.in's standardised tables are the "delivered" baseline where a results filing has not been machine-read; they come from filings, but they are not the filing itself.
- Free-tier limits shaped the design: batching, smaller reasoning prompts, waiting out overloads. A paid tier would allow fuller context per call.
- Keyword rules back up the model classifier, so the app degrades instead of failing when a model is unavailable.

## Deliberately left out

BSE announcements, intraday data, news and broker research (primary sources only), true Web Push with a service worker (polling plus browser notifications instead), working-capital, contingent-liability and related-party analysis, and user accounts.

## Where I'd take it next

An evaluation set of ~50 real filings to measure extraction accuracy per model; tracking how each guidance item is revised call to call; peer comparison within a sector; an analyst feedback loop where corrections become test cases; and Web Push so alerts reach a locked phone.
