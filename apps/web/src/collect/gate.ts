import { useRef, useState } from 'react'

import { useCollectMissing, usePlanCollections } from '../api/hooks'
import type {
  CreateCollection,
  Instrument,
  PlanCollectionRequest,
  PlannedCollection,
} from '../api/types'
import { catalogueClasses, collectionRequests, queuedKeys } from './missing'

/**
 * Ask what a launch is missing before launching — his rule (17/09): say what is missing and ask.
 *
 * `check` asks the plan; nothing missing launches at once, anything missing opens the prompt and
 * waits for the person. `dismiss` closes it, and a screen calls it whenever its form changes: a
 * plan answers the request it was asked about, not the one on screen now. ⚠️ An answer still on
 * its way when the form changes is dropped by `plan.reset()` — React Query does not call a reset
 * mutation's `onSuccess` (probed on 5.102.8), so the late answer never launches the old form.
 *
 * `collect` queues what is missing and remembers every window it queued, for as long as the
 * screen is open, so pressing again never sends one twice.
 */
export function useMissingDataGate(launch: () => void, instruments: readonly Instrument[] | undefined) {
  const plan = usePlanCollections()
  const collection = useCollectMissing()
  const [missing, setMissing] = useState<PlannedCollection[] | null>(null)
  // Windows the server took — state, because the button's label is drawn from it.
  const [queued, setQueued] = useState<ReadonlySet<string>>(() => new Set())
  // Windows sent and not yet answered — a ref, read only inside the press, because a second click
  // can arrive before the first one has re-rendered anything. It empties when the sending ends,
  // so a refused window can be tried again.
  const sending = useRef(new Set<string>())

  const classes = catalogueClasses(instruments)
  const outstanding =
    missing === null ? 0 : collectionRequests(missing, classes, queued).length

  const check = (request: PlanCollectionRequest): void => {
    setMissing(null)
    plan.mutate(request, {
      onSuccess: (items) => {
        if (items.length === 0) launch()
        else setMissing(items)
      },
    })
  }

  const dismiss = (): void => {
    setMissing(null)
    plan.reset()
    // ⚠️ Not while it is sending: the requests go on regardless, and resetting would forget how
    // many of them the server took.
    if (!collection.isPending) collection.reset()
  }

  const collect = (): void => {
    // Asked of the set now, not of the last render: a second press must see the first one's
    // windows even if the screen has not re-rendered in between.
    const skip = new Set([...queued, ...sending.current])
    const bodies: CreateCollection[] =
      missing === null ? [] : collectionRequests(missing, classes, skip)
    if (bodies.length === 0) return
    for (const body of bodies) for (const key of queuedKeys(body)) sending.current.add(key)
    collection.mutate(
      {
        bodies,
        onQueued: (body) => {
          setQueued((taken) => new Set([...taken, ...queuedKeys(body)]))
        },
      },
      {
        onSettled: () => {
          sending.current.clear()
        },
      },
    )
  }

  return { check, dismiss, missing, plan, collection, collect, outstanding }
}

export type MissingDataGate = ReturnType<typeof useMissingDataGate>
