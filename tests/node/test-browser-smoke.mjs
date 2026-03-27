import { test } from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

test('browser smoke: launch chromium and open about:blank', async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto('about:blank');
  const title = await page.title();
  // about:blank may have empty title but the call should succeed
  assert.ok(typeof title === 'string');
  await browser.close();
});
