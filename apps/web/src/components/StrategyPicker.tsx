import { useStrategies } from '../api/hooks'
import { count } from '../format'

/**
 * Choose a saved strategy to launch something over.
 *
 * ⚠️ **The screens that need this used to require a strategy created in the current browser
 * session**, because there was no way to ask the server what existed. A reload emptied the
 * store, and Study and Basket became unreachable until you built something new — for a
 * database holding forty-five strategies. That is what `GET /strategies` was missing for.
 *
 * Each option carries the **setup** beside the name, and that is not decoration: a name is
 * typed by a person and a setup is executed by the engine, so only one of them is evidence.
 * This project's own database holds `Structure — CHoCH 56454`, which runs `mme9_breakout`.
 *
 * A grid's own points are absent because the server leaves them out — a hundred-point study
 * writes a hundred strategies, and none of them is something a person picks from a list.
 */
export function StrategyPicker(props: {
  value: string
  onChange: (id: string, name: string) => void
  label?: string
}): React.JSX.Element {
  // The whole list in one request: forty-five rows is not a paging problem, and a picker that
  // paged would make the reader hunt for a strategy they know they have.
  const strategies = useStrategies({ limit: 200 })
  const items = strategies.data?.items ?? []

  return (
    <label className="flex flex-col gap-1 text-sm text-slate-300">
      {props.label ?? 'Strategy'}
      <select
        className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none"
        value={props.value}
        onChange={(event) => {
          const chosen = items.find((item) => item.id === event.target.value)
          if (chosen !== undefined) props.onChange(chosen.id, chosen.name)
        }}
      >
        <option value="">
          {strategies.isPending ? 'Loading…' : items.length === 0 ? 'Nothing saved yet' : 'Choose…'}
        </option>
        {items.map((item) => (
          <option key={item.id} value={item.id}>
            {item.name} · {item.setup ?? 'DSL'} · v{item.version} ·{' '}
            {item.runs === 0 ? 'never run' : `${count(item.runs)} run${item.runs === 1 ? '' : 's'}`}
          </option>
        ))}
      </select>
      {strategies.isError && (
        <span className="text-xs text-red-400">Could not load your strategies.</span>
      )}
    </label>
  )
}
