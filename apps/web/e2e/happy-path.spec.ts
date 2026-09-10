import { expect, test } from '@playwright/test'

import { mockApi, runToResults } from './fixtures'

// The whole user journey — build a strategy, run a backtest, read the results — in a real
// browser, with every API call fulfilled from a fixture. The point is the UI flow and its wiring
// (navigation, the poll settling on `done`, the results rendering), not the backend, which has
// its own end-to-end test in `apps/api`.
//
// ⚠️ **This file was dead long enough to name a screen that no longer exists.** Nothing in CI ran
// playwright, so it drifted: it waited on a heading `Build a strategy` and clicked a
// `save & configure` button from a two-step flow that has since become one, and it mocked
// `/api/instruments` for a market picker that now searches `/api/symbols/search`. A spec nobody
// runs is not coverage — it is a file that ages while asserting things that stopped being true.
// Repaired and wired into CI in the same change, because repairing it alone only restarts the
// clock.

test('build a strategy, run a backtest, and read the results', async ({ page }) => {
  await mockApi(page)
  await runToResults(page)

  await expect(page.getByText('done')).toBeVisible()
  // The net-profit tile, addressed by its label so it is not confused with the equal expectancy.
  await expect(page.getByText('Net profit').locator('..')).toContainText('+100.00')
  await expect(page.getByText('2.00R')).toBeVisible() // the trade's R-multiple, in the table

  // ⚠️ The second group of tiles, which is where a fixture drifting from the API shows first:
  // these nine were computed and unrendered until PR-216, and the run fixture has to carry them
  // or this reads as a screen that lost them.
  await expect(page.getByText('Gross profit').locator('..')).toContainText('200.00')
  await expect(page.getByText('Long trades').locator('..')).toContainText('1')
})
