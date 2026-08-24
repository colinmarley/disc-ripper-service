import { test, expect } from '@playwright/test';

// Read-only: the page auto-loads /jobs and /disc/status on mount (no
// hardware access), so these are safe against a real running instance.

test('page loads with title and core controls', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveTitle('Disc Ripper');
  await expect(page.getByRole('heading', { name: 'Disc Ripper' })).toBeVisible();
  await expect(page.locator('#scan-btn')).toBeVisible();
  await expect(page.locator('#scan-btn')).toHaveText('Scan Disc');
});

test('job list section renders (even with zero jobs)', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#jobs-wrap')).toBeVisible();
});

test('rip configuration form fields are present', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#f-title')).toBeVisible();
  await expect(page.locator('#f-year')).toBeVisible();
  await expect(page.locator('#f-imdb')).toBeVisible();
  await expect(page.locator('#f-media')).toBeVisible();
  await expect(page.locator('#start-btn')).toBeVisible();
});
