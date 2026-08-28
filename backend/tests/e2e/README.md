# End-to-end smoke test

This folder contains a single Playwright smoke test that drives the full
investigator journey against a running backend + frontend:

1. Log in as an analyst.
2. Submit an analysis request on the input form.
3. Wait for the progress view to reach a terminal state.
4. On the dashboard, pick up one finding, mark another as an FP override.
5. Log in as a reviewer in a second context, confirm the remaining match.
6. Sign the case off.
7. Download the evidence packet and assert the `Content-Disposition`
   filename is present.

The test intentionally covers only the golden path — regression tests for
edge cases belong in the unit and integration suites. This is a "does the
whole thing still work end-to-end after a refactor?" gate.

## Why Playwright rather than Cypress

Playwright supports multi-context sessions natively (needed for the
analyst → reviewer hand-off) and has a first-class API for file downloads.

## Prerequisites

```bash
# Backend: SQLite DB pre-seeded with the three canonical users and the
# sanctions lists loaded via `python update_lists.py`.
SANCTIONSIGHT_DB_PATH=/tmp/e2e.db \
SANCTIONSIGHT_AUDIT_DIR=/tmp/e2e-audit \
SANCTIONSIGHT_JWT_SECRET=e2e-secret \
GOOGLE_API_KEY=fake GOOGLE_CSE_ID=fake GOOGLE_GENAI_API_KEY=fake \
uvicorn main:app --port 8000

# Frontend: built and served from dist/
cd ../frontend && npm run build && npm run preview -- --port 5173

# Playwright
npm init -y
npm install --save-dev @playwright/test
npx playwright install --with-deps chromium
```

## Running

```bash
npx playwright test tests/e2e/smoke.spec.ts
```

CI wires these into a single job that starts the backend with a stubbed
Google CSE (see `conftest.py::stubbed_pipeline`) and mocks the LLM to a
deterministic verdict.

## Status

The spec file (`smoke.spec.ts`) is scaffolded. It is marked `test.skip()`
by default so CI doesn't fail on a missing Playwright install. Remove
the skip and add Playwright to CI once the stubbed-backend harness is
wired up (tracked in the Phase 7 backlog).
