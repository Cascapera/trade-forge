import { validateStrategy } from '@tradeforge/schema'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { useCreateStrategy } from '../api/hooks'
import { useSession } from '../store'
import {
  buildStrategy,
  maCrossForm,
  OPS,
  SOURCES,
  TIMEFRAMES,
  type ConditionRow,
  type IndicatorForm,
  type SideForm,
  type StrategyForm,
} from '../strategy/builder'

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'
const sectionClass = 'rounded-lg border border-slate-800 bg-slate-900/40 p-4'

function ConditionRows(props: {
  label: string
  side: SideForm
  onChange: (next: SideForm) => void
}): React.JSX.Element {
  const { label, side, onChange } = props
  const setRow = (index: number, patch: Partial<ConditionRow>): void => {
    onChange({ ...side, rows: side.rows.map((row, i) => (i === index ? { ...row, ...patch } : row)) })
  }
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-3">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={side.enabled}
            onChange={(event) => {
              onChange({ ...side, enabled: event.target.checked })
            }}
          />
          {label}
        </label>
        {side.rows.length > 1 && (
          <select
            aria-label={`${label} combine`}
            className={inputClass}
            value={side.combine}
            onChange={(event) => {
              onChange({ ...side, combine: event.target.value as SideForm['combine'] })
            }}
          >
            <option value="all">match all</option>
            <option value="any">match any</option>
          </select>
        )}
      </div>
      {side.enabled &&
        side.rows.map((row, index) => (
          <div key={index} className="flex items-center gap-2">
            <input
              aria-label={`${label} left ${String(index)}`}
              className={inputClass}
              placeholder="fast"
              value={row.left}
              onChange={(event) => {
                setRow(index, { left: event.target.value })
              }}
            />
            <select
              aria-label={`${label} op ${String(index)}`}
              className={inputClass}
              value={row.op}
              onChange={(event) => {
                setRow(index, { op: event.target.value as ConditionRow['op'] })
              }}
            >
              {OPS.map((op) => (
                <option key={op} value={op}>
                  {op}
                </option>
              ))}
            </select>
            <input
              aria-label={`${label} right ${String(index)}`}
              className={inputClass}
              placeholder="slow"
              value={row.right}
              onChange={(event) => {
                setRow(index, { right: event.target.value })
              }}
            />
            <button
              type="button"
              className="text-slate-500 hover:text-red-400"
              onClick={() => {
                onChange({ ...side, rows: side.rows.filter((_, i) => i !== index) })
              }}
            >
              remove
            </button>
          </div>
        ))}
      {side.enabled && (
        <button
          type="button"
          className="text-sm text-sky-400 hover:text-sky-300"
          onClick={() => {
            onChange({ ...side, rows: [...side.rows, { left: '', op: 'gt', right: '' }] })
          }}
        >
          + condition
        </button>
      )}
    </div>
  )
}

export function StrategyBuilder(): React.JSX.Element {
  const [form, setForm] = useState<StrategyForm>(maCrossForm)
  const navigate = useNavigate()
  const setStrategy = useSession((state) => state.setStrategy)
  const create = useCreateStrategy()

  const document = useMemo(() => buildStrategy(form), [form])
  const validation = useMemo(() => validateStrategy(document), [document])

  const patch = (update: Partial<StrategyForm>): void => {
    setForm({ ...form, ...update })
  }

  const save = (): void => {
    create.mutate(document, {
      onSuccess: (strategy) => {
        setStrategy(strategy.id, strategy.name)
        void navigate('/launch')
      },
    })
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold">Build a strategy</h2>
        <button
          type="button"
          className="text-sm text-slate-400 hover:text-slate-200"
          onClick={() => {
            setForm(maCrossForm())
          }}
        >
          Load MA-cross template
        </button>
      </div>

      <section className={sectionClass}>
        <div className="flex flex-wrap gap-4">
          <label className="flex flex-col gap-1 text-sm">
            Name
            <input
              aria-label="name"
              className={inputClass}
              value={form.name}
              onChange={(event) => {
                patch({ name: event.target.value })
              }}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Timeframe
            <select
              aria-label="timeframe"
              className={inputClass}
              value={form.timeframe}
              onChange={(event) => {
                patch({ timeframe: event.target.value as StrategyForm['timeframe'] })
              }}
            >
              {TIMEFRAMES.map((tf) => (
                <option key={tf} value={tf}>
                  {tf}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Risk % per trade
            <input
              aria-label="percent"
              type="number"
              step="0.1"
              className={inputClass}
              value={form.percent}
              onChange={(event) => {
                patch({ percent: Number(event.target.value) })
              }}
            />
          </label>
        </div>
      </section>

      <section className={sectionClass}>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="font-medium">Indicators</h3>
          <button
            type="button"
            className="text-sm text-sky-400 hover:text-sky-300"
            onClick={() => {
              patch({
                indicators: [
                  ...form.indicators,
                  { id: '', kind: 'SMA', period: 14, source: 'close' },
                ],
              })
            }}
          >
            + indicator
          </button>
        </div>
        <div className="space-y-2">
          {form.indicators.map((indicator, index) => (
            <IndicatorRow
              key={index}
              indicator={indicator}
              onChange={(next) => {
                patch({
                  indicators: form.indicators.map((item, i) => (i === index ? next : item)),
                })
              }}
              onRemove={() => {
                patch({ indicators: form.indicators.filter((_, i) => i !== index) })
              }}
            />
          ))}
        </div>
      </section>

      <section className={sectionClass}>
        <h3 className="mb-2 font-medium">Entry</h3>
        <div className="space-y-4">
          <ConditionRows
            label="Long"
            side={form.long}
            onChange={(next) => {
              patch({ long: next })
            }}
          />
          <ConditionRows
            label="Short"
            side={form.short}
            onChange={(next) => {
              patch({ short: next })
            }}
          />
        </div>
      </section>

      <section className={sectionClass}>
        <h3 className="mb-2 font-medium">Exit</h3>
        <div className="mb-3 flex flex-wrap gap-4">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={form.stop.enabled}
              onChange={(event) => {
                patch({ stop: { ...form.stop, enabled: event.target.checked } })
              }}
            />
            Stop at candle extreme
          </label>
          {form.stop.enabled && (
            <>
              <input
                aria-label="stop lookback"
                type="number"
                className={inputClass}
                value={form.stop.lookback}
                onChange={(event) => {
                  patch({ stop: { ...form.stop, lookback: Number(event.target.value) } })
                }}
              />
              <select
                aria-label="stop side"
                className={inputClass}
                value={form.stop.side}
                onChange={(event) => {
                  patch({ stop: { ...form.stop, side: event.target.value as 'low' | 'high' } })
                }}
              >
                <option value="low">low</option>
                <option value="high">high</option>
              </select>
            </>
          )}
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={form.takeProfit.enabled}
              onChange={(event) => {
                patch({ takeProfit: { ...form.takeProfit, enabled: event.target.checked } })
              }}
            />
            Take profit at R:R
          </label>
          {form.takeProfit.enabled && (
            <input
              aria-label="take profit rr"
              type="number"
              step="0.1"
              className={inputClass}
              value={form.takeProfit.rr}
              onChange={(event) => {
                patch({ takeProfit: { ...form.takeProfit, rr: Number(event.target.value) } })
              }}
            />
          )}
        </div>
        <ConditionRows
          label="Exit conditions"
          side={form.exit}
          onChange={(next) => {
            patch({ exit: next })
          }}
        />
      </section>

      {!validation.valid && (
        <section className="rounded-lg border border-amber-800 bg-amber-950/40 p-4 text-sm">
          <p className="mb-2 font-medium text-amber-300">This strategy is not valid yet:</p>
          <ul className="list-inside list-disc text-amber-200">
            {validation.errors.map((error, index) => (
              <li key={index}>
                <span className="font-mono">{error.path}</span> {error.message}
              </li>
            ))}
          </ul>
        </section>
      )}

      {create.isError && (
        <p className="text-sm text-red-400">The API rejected the strategy. Check the fields above.</p>
      )}

      <button
        type="button"
        disabled={!validation.valid || create.isPending}
        onClick={save}
        className="rounded bg-sky-600 px-4 py-2 font-medium text-white enabled:hover:bg-sky-500 disabled:opacity-40"
      >
        {create.isPending ? 'Saving…' : 'Save & configure backtest'}
      </button>
    </div>
  )
}

function IndicatorRow(props: {
  indicator: IndicatorForm
  onChange: (next: IndicatorForm) => void
  onRemove: () => void
}): React.JSX.Element {
  const { indicator, onChange, onRemove } = props
  return (
    <div className="flex items-center gap-2">
      <input
        aria-label="indicator id"
        className={inputClass}
        placeholder="id"
        value={indicator.id}
        onChange={(event) => {
          onChange({ ...indicator, id: event.target.value })
        }}
      />
      <select
        aria-label="indicator kind"
        className={inputClass}
        value={indicator.kind}
        onChange={(event) => {
          onChange({ ...indicator, kind: event.target.value as IndicatorForm['kind'] })
        }}
      >
        <option value="SMA">SMA</option>
        <option value="EMA">EMA</option>
      </select>
      <input
        aria-label="indicator period"
        type="number"
        className={inputClass}
        value={indicator.period}
        onChange={(event) => {
          onChange({ ...indicator, period: Number(event.target.value) })
        }}
      />
      <select
        aria-label="indicator source"
        className={inputClass}
        value={indicator.source}
        onChange={(event) => {
          onChange({ ...indicator, source: event.target.value as IndicatorForm['source'] })
        }}
      >
        {SOURCES.map((source) => (
          <option key={source} value={source}>
            {source}
          </option>
        ))}
      </select>
      <button type="button" className="text-slate-500 hover:text-red-400" onClick={onRemove}>
        remove
      </button>
    </div>
  )
}
