import { expect, test } from '@playwright/test'

import { mockApi, runToResults } from './fixtures'

// Captures the results screen for the README. A tool rather than a check: it asserts only enough
// to know the page is the one it means to photograph.
//
// ⚠️ **Not run in CI, and deliberately.** It writes into `docs/assets/`, so a CI run would either
// commit a screenshot nobody asked for or fail on a dirty tree. Run it by hand when the results
// screen changes enough that the picture in the README stops being true.
test('capture the results screen for the README', async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 1000 })
  await mockApi(page)
  await runToResults(page)

  await expect(page.getByText('2.00R')).toBeVisible()
  // Let the equity chart finish its first paint before capturing.
  await page.waitForTimeout(500)
  await page.screenshot({ path: '../../docs/assets/results.png', fullPage: true })
})
