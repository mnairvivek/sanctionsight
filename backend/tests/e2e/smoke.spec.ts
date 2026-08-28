/**
 * End-to-end smoke test — golden path only.
 *
 * Runs the full investigator journey with the backend and frontend both
 * actually running. Scoped deliberately narrow so this is a go/no-go
 * signal, not a regression harness.
 *
 * See tests/e2e/README.md for setup. Skipped by default; remove the
 * `test.skip()` guard once the stubbed-backend harness is wired into CI.
 */
import { test, expect, Page } from '@playwright/test';

const BASE_URL = process.env.SANCTIONSIGHT_E2E_URL ?? 'http://localhost:5173';
const API_URL = process.env.SANCTIONSIGHT_E2E_API_URL ?? 'http://localhost:8000';

const ANALYST = { email: 'analyst@test', password: 'analyst-pw' };
const REVIEWER = { email: 'reviewer@test', password: 'reviewer-pw' };

async function login(page: Page, user: { email: string; password: string }) {
  await page.goto(`${BASE_URL}/app`);
  await page.getByLabel(/email/i).fill(user.email);
  await page.getByLabel(/password/i).fill(user.password);
  await page.getByRole('button', { name: /sign in|log in/i }).click();
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
}

test.describe('SanctionSight smoke', () => {
  // Enable locally once the E2E harness is stood up (see README.md).
  test.skip(!process.env.SANCTIONSIGHT_E2E_ENABLED, 'E2E harness not configured');

  test('input → brief → review → sign-off → export', async ({ browser }) => {
    // ------------------------------------------------------------------
    // Analyst kicks off the investigation.
    // ------------------------------------------------------------------
    const analystCtx = await browser.newContext();
    const analystPage = await analystCtx.newPage();
    await login(analystPage, ANALYST);

    await analystPage.getByLabel(/website/i).fill('example.com');
    await analystPage.getByLabel(/business name/i).fill('Example Corp');
    await analystPage
      .getByRole('button', { name: /run analysis|analyze|start/i })
      .click();

    // Wait for the progress view to reach a terminal state (backend
    // emits 'completed' via SSE; progress bar hits 100%).
    await expect(
      analystPage.getByText(/completed|dashboard ready|analysis complete/i),
    ).toBeVisible({ timeout: 120_000 });

    // ------------------------------------------------------------------
    // Dashboard: analyst triages findings.
    // ------------------------------------------------------------------
    const firstFinding = analystPage
      .getByRole('row')
      .filter({ hasText: /iran|cuba|russia/i })
      .first();
    await firstFinding.getByRole('button', { name: /review|open/i }).click();

    // FP-override the low-risk finding.
    await analystPage
      .getByRole('button', { name: /false positive|mark fp/i })
      .click();
    await analystPage
      .getByPlaceholder(/reason|justification/i)
      .fill('Photograph — benign travel content');
    await analystPage
      .getByRole('button', { name: /confirm|submit/i })
      .click();

    // ------------------------------------------------------------------
    // Reviewer signs off in a separate context.
    // ------------------------------------------------------------------
    const reviewerCtx = await browser.newContext();
    const reviewerPage = await reviewerCtx.newPage();
    await login(reviewerPage, REVIEWER);

    // Navigate to the same case — URL is stable across contexts.
    const caseUrl = analystPage.url();
    await reviewerPage.goto(caseUrl);

    await reviewerPage
      .getByRole('button', { name: /confirm match/i })
      .first()
      .click();

    await reviewerPage.getByRole('button', { name: /sign off/i }).click();
    await reviewerPage
      .getByPlaceholder(/final disposition|sign-off notes/i)
      .fill('Confirmed Tehran connection; Cuba cleared.');
    await reviewerPage
      .getByRole('button', { name: /sign off|confirm/i })
      .last()
      .click();

    await expect(reviewerPage.getByText(/signed off/i)).toBeVisible();

    // ------------------------------------------------------------------
    // Evidence packet download.
    // ------------------------------------------------------------------
    const downloadPromise = reviewerPage.waitForEvent('download');
    await reviewerPage
      .getByRole('button', { name: /evidence packet|download packet/i })
      .click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/\.zip$/);

    await analystCtx.close();
    await reviewerCtx.close();
  });
});
