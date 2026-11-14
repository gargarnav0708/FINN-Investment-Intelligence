# AI build history

The brief asks to see how the tools were used, not just the result. This file indexes the conversations and summarises what each phase did, with the commits it produced.

## Conversation exports and links

| Phase | Tool | Link / export |
|---|---|---|
| First build: app, ingestion, research views, Thesis Drift (commits `6a90d1b` → `229ae76`, 11–13 Nov) | _add tool used_ | _add share link or export file_ |
| Audit against the brief, generic pipeline, push intelligence, copilot, deployment (commits `c967f81` → latest, 13–14 Nov) | Claude (Claude Code on the web) | https://claude.ai/code/session_01PG4gH8nFykeSM9iZR2JUGV |

## How the second phase went

**1. Audit before building.** The agent read the brief, the reference taxonomy and the repo, ran the test suite, and produced a done/missing table and a P0–P3 priority list. The finding that shaped everything: the core workflows only worked for Tata Power (issuer-specific scrapers, hard-coded management names, numbers gated behind manual validation), and Live mode crashed on load because three frontend functions were called but never defined.

**2. P0: make it generic and verifiable** (`c967f81`). A two-tier model router with a response cache; results-filing extraction with mechanical checks replacing manual validation; screener.in as a document directory for any ticker; guidance extraction with verbatim quote grounding; promised-vs-delivered with number checks; Render deployment with a pre-built database snapshot (NSE blocks cloud IPs). The sandbox could not reach NSE, screener.in or any model API, so everything was built against fixtures and offline tests, and screenshots were taken at phone width with headless Chrome.

**3. First contact with real data** (`523eeb7` → `e7bfd9f`). Running on a real machine surfaced problems that offline tests could not:
- Gemini 2.5 models were no longer offered to new keys → switched to 3.5 Flash-Lite / 3.8 Flash after listing the models the key can use.
- The newer models rejected a "thinking" parameter with a generic HTTP 400 → retry without optional parameters on any 400.
- 503 "high demand" from Gemini → fallback models, then waiting in rounds.
- "database is locked" when the CLI and web app ran together → SQLite WAL mode and a lock timeout.
- screener.in labels annual reports "Annual Report 2026", not "Financial Year 2026" → found by printing the live page structure, parser fixed, fixture updated.
- Real quotes failed verbatim matching because PDF text breaks sentences with hyphenation and page headers → matching on letters and digits with long in-order runs (paraphrases still fail).
- The assessment model marked FY26 targets "pending" after FY26 had ended → the prompt now gets today's date and the latest full year.

**4. Push intelligence and research views** (`25ca033`). The reference 17-category taxonomy with batched classification and one-line summaries; management-change profiles from the filing with optional web background; business snapshot and bull vs bear from primary documents; phone-first Today cards; the "Ask FINN" copilot; a weekday end-of-day scheduler with a GitHub Actions trigger for the sleeping free host; notifications.

**5. Living within free tiers** (`23b0e7c` → `4a61ac3`). Gemini Flash's free daily quota ran out during testing. Rather than pay, the reasoning tier moved to Groq (gpt-oss-120b, free, ~8k tokens/minute): each tier can now point at its own provider, prompts are sized to the provider's budget, and the assessment is judged in batches so every guidance statement gets a verdict.

**6. Closing the gaps against the brief** (this commit). A five-year fundamentals view (growth, margins, debt, cash conversion) for any company, an installable web app, this file and the writeup.

## Working pattern

- The agent asked before anything costly or irreversible (which LLM provider, hosting, how to deliver code, whether to spend money on a paid tier) and decided routine things itself.
- Keys never went through the chat: they live in `.env` locally and in the host's environment settings.
- Every live run's JSON report was pasted back and read line by line; each fix came with a regression test (71 tests at the end of phase 6).
