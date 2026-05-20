/**
 * Browser smoke test — proves whether the current environment has the
 * system/runtime dependencies required to launch Playwright's Chromium.
 *
 * Pass:  host with Playwright and Chromium installed.
 * Skip:  environments without Playwright installed.
 *
 * Run:
 *   node --test tests/node/test-browser-smoke.mjs
 *   npm run test:smoke:node
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

let chromium;
let playwrightImportError;
try {
  ({ chromium } = await import('playwright'));
} catch (error) {
  playwrightImportError = error;
}

if (playwrightImportError) {
  test('Chromium smoke test skipped when playwright is unavailable', { skip: 'playwright not installed in this environment' }, () => {});
} else {
  test('Chromium launches headlessly and can navigate to about:blank', async () => {
    const browser = await chromium.launch({ headless: true });
    try {
      const page = await browser.newPage();
      await page.goto('about:blank');
      const title = await page.title();
      assert.notEqual(title, null, 'page.title() should not be null');
      assert.notEqual(title, undefined, 'page.title() should not be undefined');
    } finally {
      await browser.close();
    }
  });
}
