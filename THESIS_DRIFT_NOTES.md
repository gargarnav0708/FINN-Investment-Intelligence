# FINN Thesis Drift Monitor

## Implemented

- `GET /api/research/{ticker}/thesis-drift` computes signals on demand. No schema change or persisted snapshot table was added.
- The Research view has a compact “What changed?” summary, evidence-linked signal groups and explicit coverage gaps.
- Supported first slice: revenue versus PAT growth divergence, PBT/PAT margins, OCF/PAT, free cash flow, net debt, borrowings/equity and current ratio.
- Only validated observations of the selected issuer, statement basis and period are used. Evidence links retain observation ID, report title, page, row, column and source quote.
- The trajectory explains when fewer than four verified periods are shown, including Infosys’s current two-quarter series.

## Formulas and interpretation

- Revenue/PAT divergence = PAT growth minus Revenue from Operations growth. Quarterly comparisons use the same quarter year over year to avoid seasonal QoQ interpretation.
- PBT and PAT margin = the respective reported metric divided by Revenue from Operations.
- Cash conversion = operating cash flow / positive PAT.
- Free cash flow = operating cash flow + the source-reported capex cash-flow amount; the source sign is retained.
- Net debt = current and non-current borrowings less cash. Debt/equity excludes lease liabilities. Current ratio = current assets / current liabilities.
- Relative moves under 1% and absolute FCF/net-debt moves under INR 1 crore are treated as no material change. No company-health score is produced. “Watch” and “needs review” signals require human review.

## Tata Power evidence

The local database contains 177 Tata Power observations, 129 validated, including three annual periods. The first monitor uses consolidated FY2023–24 through FY2025–26 observations:

- Revenue/PAT growth: annual report pages 416, 392 and 245.
- PBT/PAT margins: pages 392 and 245.
- OCF/PAT and free cash flow: pages 395 and 247 (FY25 and FY26).
- Net debt, debt/equity and current ratio: pages 390–391 and 244–245.

EBITDA is unavailable where it is not separately disclosed. Working-capital measures are withheld until compatible inventory, receivables, payables and denominator observations are validated.

## Coverage beyond Tata Power

- Infosys has nine validated observations: Q4 FY2024–25 and Q4 FY2025–26 plus three FY2025–26 headline annual values. It lacks validated cash-flow and balance-sheet observations; its annual report is indexed but its financial statements have not been extracted. The chart’s two quarterly points are a coverage limit, not a visual rendering failure.
- HINDUNILVR, KOTAKBANK, BEL and ULTRACEMCO have no validated financial observations in the inspected DB; the API reports insufficient coverage.
- Existing announcement classifications support limited capital/governance triage. A category match is marked for review and is not described as new, adverse or material. Auditor opinions, related-party transactions, contingent liabilities, pledging, insider trades and rating changes are not extracted in this slice.

## Test and UI verification

- Tests cover source-linked formulas, signed capex, issuer isolation and missing-data handling.
- Full backend suite: **26 passed**. `node --check app.js` passed.
- TestClient checks returned HTTP 200 for Tata Power, Infosys and HINDUNILVR. Tata Power had 3/5 signal groups with coverage; Infosys had partial earnings/disclosure coverage; HINDUNILVR returned 0/5.
- Browser QA checked Tata Power and Infosys in live Research. Responsive checks at 375, 390, 768 and 1440 CSS pixels found no horizontal overflow; cards stack on mobile. Temporary viewport overrides were reset.

## Remaining work

Full working-capital calculations; extracted governance evidence; canonical announcement taxonomy; Thesis Drift retrieval in the research assistant; deeper Push-to-monitor integration; broader issuer extraction; and deployment/persistent-host verification are not implemented. These are not claimed as supported.
