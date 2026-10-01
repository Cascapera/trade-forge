import { Link } from 'react-router-dom'

import type { WindowUse } from '../api/types'

function day(iso: string): string {
  return iso.slice(0, 10)
}

/** Where an earlier look can be read: the test itself, or the walk-forward its fold belongs to. */
function UseLink(props: { use: WindowUse }): React.JSX.Element {
  const { use } = props
  if (use.kind === 'holdout') {
    return (
      <Link to={`/sweeps/${use.id}`} className="text-sky-400 hover:text-sky-300">
        Reserved-window test
      </Link>
    )
  }
  return (
    <Link to={`/sweep-walkforwards/${use.id}`} className="text-sky-400 hover:text-sky-300">
      Walk-forward, fold {String((use.fold ?? 0) + 1)}
    </Link>
  )
}

/** The earlier looks at a reserved window (01/10): which test, over which window, launched when. */
export function WindowUseList(props: { uses: WindowUse[] }): React.JSX.Element {
  return (
    <ul aria-label="earlier tests of this window" className="list-disc pl-5">
      {props.uses.map((use) => (
        <li key={`${use.id}-${String(use.fold ?? '')}`}>
          <UseLink use={use} /> · tested {day(use.date_from)} → {day(use.date_to)} · launched{' '}
          {day(use.created_at)}
        </li>
      ))}
    </ul>
  )
}

/**
 * What a launcher shows when the server refuses a window already used (01/10): the sentence, the
 * earlier tests by name, and the one box that sends the launch again as a retest.
 *
 * ⚠️ **Unticked until ticked by hand, every time.** The reserved window is used once; a second
 * look is a choice made on it, and a box ticked for the reader would make that choice silently.
 */
export function RetestPrompt(props: {
  message: string
  uses: WindowUse[]
  retest: boolean
  onRetest: (retest: boolean) => void
}): React.JSX.Element {
  return (
    <div role="alert" className="space-y-2 rounded border border-amber-700 p-3 text-sm text-amber-300">
      <p>{props.message}</p>
      <WindowUseList uses={props.uses} />
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={props.retest}
          onChange={(event) => {
            props.onRetest(event.target.checked)
          }}
        />
        Test again (retest) — this window was already used
      </label>
    </div>
  )
}
