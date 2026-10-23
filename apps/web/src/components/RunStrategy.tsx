import { useStrategy } from '../api/hooks'
import type { BacktestPoint } from '../api/types'
import { describeParameters, setupName } from '../strategy/describe'

/**
 * The top of a result, in words (29/09, his ask): which strategy ran, and every parameter it ran
 * with — the ones its sweep varied at this point marked as optimised.
 *
 * ⚠️ **Read from the document the run executed**, fetched by its id, never from a label: a sweep's
 * point is a strategy row of its own, so the values here are the point's. The catalogue name leads
 * when the run came from an entry, because that is how he knows it; the document's name follows.
 */
export function RunStrategy(props: {
  strategyId: string
  point: BacktestPoint | null | undefined
}): React.JSX.Element | null {
  const strategy = useStrategy(props.strategyId)
  const document = strategy.data
  if (document === undefined) return null

  const point = props.point ?? null
  const setup = (document.definition.setup as { type?: unknown } | null | undefined)?.type
  const rows = describeParameters(document.definition, point?.values ?? {})
  const optimised = rows.filter((one) => one.optimised).length

  return (
    <section
      aria-label="strategy of this run"
      className="flex flex-col gap-3 rounded-lg border border-slate-800 p-4"
    >
      <div className="flex flex-col gap-1">
        <p className="text-base">
          <span className="font-semibold text-slate-100">
            {point?.entry_name ?? document.name}
          </span>{' '}
          <span className="text-slate-400">
            · {setupName(typeof setup === 'string' ? setup : null)}
          </span>
        </p>
        <p className="text-xs text-slate-500">
          documento {document.name} v{document.version}
          {point !== null &&
            ` · ponto de uma varredura${optimised > 0 ? ` · ${String(optimised)} otimizado${optimised === 1 ? '' : 's'}` : ''}`}
        </p>
      </div>
      <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-2 lg:grid-cols-3">
        {rows.map((one) => (
          <div
            key={one.path}
            className={`flex justify-between gap-3 border-b border-slate-900 py-0.5 ${
              one.optimised ? 'text-sky-200' : ''
            }`}
          >
            <dt className={one.optimised ? 'text-sky-300' : 'text-slate-400'}>{one.label}</dt>
            <dd className="text-right">
              {one.value}
              {one.optimised && (
                <span className="ml-2 rounded bg-sky-900/60 px-1.5 text-xs text-sky-200">
                  otimizado
                </span>
              )}
              {one.byDefault && <span className="ml-2 text-xs text-slate-500">(padrão)</span>}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
