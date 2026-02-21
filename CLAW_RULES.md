# Trade Labs (U.T.S.) — Copilot/Claw Operating Rules

Repo: ~/trade-labs

## Mission
Refactor + improve efficiency + improve data quality WITHOUT breaking tests.

## Hard Rules
1) Do NOT modify anything that can place live trades (broker execution) unless explicitly instructed.
2) Keep changes small and reviewable (1–5 files per change).
3) After ANY change, run:
   - ./scripts/dev.sh
4) Always provide:
   - Summary of changes
   - git diff
   - test output
5) No secrets. Do not print or store API keys.

## Allowed Work
- Refactors, cleanup, performance improvements, caching, better logging
- Unit test improvements
- CLI wrappers and report generation