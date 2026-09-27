import { useState } from 'react'

import { usePlanCollections } from '../api/hooks'
import type { PlanCollectionRequest, PlannedCollection } from '../api/types'
import { anythingToCollect } from './missing'

/**
 * Ask what a launch is missing before launching, and collect it without asking (26/09).
 *
 * `check` asks the plan; nothing missing launches at once, and anything the broker can send is
 * collected first — his rule of 26/09, which replaced "say what is missing and ask" (17/09): the
 * prompt cost a click on every launch and the answer was always "collect". It still opens when
 * **nothing** missing can be collected, because then there is something to read before running:
 * why the window will be shorter, or why nothing would run at all. `dismiss` closes it, and a screen calls it whenever its form changes: a
 * plan answers the request it was asked about, not the one on screen now. ⚠️ An answer still on
 * its way when the form changes is dropped by `plan.reset()` — React Query does not call a reset
 * mutation's `onSuccess` (probed on 5.102.8), so the late answer never launches the old form.
 *
 * ⚠️ **The gate does not collect anything.** Queueing the downloads is the launch's job: both
 * screens send `collect_missing` (PR-266 and PR-267), and the server plans the windows, writes
 * the collections and links them to the runs. A screen that queued them itself would be a second
 * author of the same plan — and the one that used to could not tell a window it had already
 * asked for from a new one.
 */
export function useMissingDataGate(launch: (collectMissing: boolean) => void) {
  const plan = usePlanCollections()
  const [missing, setMissing] = useState<PlannedCollection[] | null>(null)

  const check = (request: PlanCollectionRequest): void => {
    setMissing(null)
    plan.mutate(request, {
      onSuccess: (items) => {
        if (items.length === 0) launch(false)
        else if (anythingToCollect(items)) launch(true)
        else setMissing(items)
      },
    })
  }

  const dismiss = (): void => {
    setMissing(null)
    plan.reset()
  }

  return { check, dismiss, missing, plan }
}

export type MissingDataGate = ReturnType<typeof useMissingDataGate>
