import {
  SETUPS,
  setupSpec,
  validateStrategy,
  type SetupParam,
  type SetupType,
} from '@tradeforge/schema'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { useCreateBacktest, useInstruments, useSaveStrategy } from '../api/hooks'
import { emptyBacktestForm, toBacktestRequest, whyNotRunnable, type BacktestForm } from '../backtest/settings'
import { BacktestSettings } from '../components/BacktestSettings'
import { NumberStepper } from '../components/NumberStepper'
import { useSession } from '../store'
import {
  buildStrategy,
  INDICATOR_KINDS,
  OPS,
  setupValues,
  SOURCES,
  STRATEGY_CHOICES,
  strategyChoice,
  TIMEFRAMES,
  type ConditionRow,
  type IndicatorForm,
  type SetupForm,
  type SideForm,
  type StrategyChoice,
  type StrategyForm,
} from '../strategy/builder'

/** The headings the picker groups by, in the order they are offered. */
const GROUPS: readonly StrategyChoice['group'][] = ['Setups', 'Conditions']

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
            <select
              aria-label={`${label} right kind ${String(index)}`}
              className={inputClass}
              value={row.rightKind}
              onChange={(event) => {
                setRow(index, { rightKind: event.target.value as ConditionRow['rightKind'] })
              }}
            >
              <option value="ref">ref</option>
              <option value="value">value</option>
            </select>
            <input
              aria-label={`${label} right ${String(index)}`}
              className={inputClass}
              type={row.rightKind === 'value' ? 'number' : 'text'}
              placeholder={row.rightKind === 'value' ? '30' : 'slow'}
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
            onChange({
              ...side,
              rows: [...side.rows, { left: '', op: 'gt', right: '', rightKind: 'ref' }],
            })
          }}
        >
          + condition
        </button>
      )}
    </div>
  )
}

/** What an empty box means for this parameter, said out loud — the distinction is invisible
 *  otherwise, and it is the difference between two different experiments. */
function emptyHint(param: SetupParam): string | null {
  if (param.kind === 'boolean' || !('nullable' in param) || !param.nullable) return null
  return param.name === 'max_bos' ? 'empty = uncapped' : 'empty = off'
}

function SetupField(props: {
  param: SetupParam
  value: string | boolean | undefined
  onChange: (next: string | boolean) => void
}): React.JSX.Element {
  const { param, value, onChange } = props
  const hint = emptyHint(param)
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span>
        {param.name}
        {param.required && <span className="text-amber-400"> *</span>}
        {hint !== null && <span className="ml-1 text-xs text-slate-500">({hint})</span>}
      </span>
      {param.kind === 'boolean' ? (
        <input
          aria-label={`setup ${param.name}`}
          type="checkbox"
          className="self-start"
          checked={value === true}
          onChange={(event) => {
            onChange(event.target.checked)
          }}
        />
      ) : param.kind === 'enum' ? (
        <select
          aria-label={`setup ${param.name}`}
          className={inputClass}
          value={typeof value === 'string' ? value : ''}
          onChange={(event) => {
            onChange(event.target.value)
          }}
        >
          {/* A required parameter with no schema default starts unanswered, and the blank option is
              how it stays that way until the user chooses. Pre-selecting `long` would turn a
              forgotten choice into a whole long-only backtest read as the setup's result. */}
          {param.default === null && <option value="">choose…</option>}
          {param.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      ) : (
        // ⚠️ Arrows that know the parameter, replacing `<input min={param.min}>` — which ignored
        // exclusivity and so let `breakeven_at_r` be stepped to exactly 0, the one value in its
        // range the API refuses. #106 fixed that sentence in the hint and left it live in the
        // field. The step was a constant `0.1` for every fraction, too.
        <NumberStepper
          param={param}
          label={`setup ${param.name}`}
          value={typeof value === 'string' ? value : ''}
          onChange={onChange}
        />
      )}
    </label>
  )
}

function SetupFields(props: {
  setup: SetupForm
  onChange: (next: SetupForm) => void
}): React.JSX.Element {
  const { setup, onChange } = props
  // Straight off the schema: kind, bounds, nullability and default. A parameter added to the DSL
  // in Python shows up here once the types are regenerated, with no edit to this file.
  const spec = setupSpec(setup.type)
  return (
    <div className="space-y-3">
      <label className="flex flex-col gap-1 text-sm">
        Setup
        <select
          aria-label="setup type"
          className={inputClass}
          value={setup.type}
          onChange={(event) => {
            // A different setup has different parameters, so the values start from its own
            // defaults rather than carrying over names that mean something else.
            const type = event.target.value as SetupType
            onChange({ type, values: setupValues(type) })
          }}
        >
          {SETUPS.map((candidate) => (
            <option key={candidate.type} value={candidate.type}>
              {candidate.type}
            </option>
          ))}
        </select>
      </label>
      <div className="flex flex-wrap gap-4">
        {spec.params.map((param) => (
          <SetupField
            key={param.name}
            param={param}
            value={setup.values[param.name]}
            onChange={(next) => {
              onChange({ ...setup, values: { ...setup.values, [param.name]: next } })
            }}
          />
        ))}
      </div>
    </div>
  )
}

export function StrategyBuilder(): React.JSX.Element {
  const [choiceId, setChoiceId] = useState(STRATEGY_CHOICES[0]?.id ?? '')
  // The clock enters here and nowhere deeper. Every form factory takes the instant as an argument,
  // so the name a run is saved under is decided at the moment the strategy is *picked* — see
  // `runName` for why that timing is what keeps versioning working.
  const [form, setForm] = useState<StrategyForm>(() => strategyChoice(choiceId).form(new Date()))
  const [backtest, setBacktest] = useState<BacktestForm>(emptyBacktestForm)
  const navigate = useNavigate()
  const session = useSession()
  const instruments = useInstruments()
  const save = useSaveStrategy()
  const run = useCreateBacktest()

  const document = useMemo(() => buildStrategy(form), [form])
  const validation = useMemo(() => validateStrategy(document), [document])
  const blocked = whyNotRunnable(backtest)

  const patch = (update: Partial<StrategyForm>): void => {
    setForm({ ...form, ...update })
  }

  /**
   * Save, then enqueue, then go and watch it.
   *
   * The save is a `PUT` whenever this name has been saved before, because the API writes version
   * 1 on every `POST` and (name, version) is unique — so re-running after nudging a parameter is
   * a *new version* of the same strategy, which is what the lineage in the database was built
   * for. Under a new name it is a `POST` and a new lineage.
   *
   * ⚠️ **"Has been saved before" is now asked of the server, and that is what removes the 409.**
   * This used to compare the typed name against the one *this tab* had created, so a strategy
   * saved in another tab, or before a reload, was invisible — and saving under its name was a
   * `POST` onto a name that already had a version 1. The lookup lives in `useSaveStrategy`, so
   * every caller of it gets the same answer.
   */
  const launch = (): void => {
    save.mutate(
      { definition: document },
      {
        onSuccess: (strategy) => {
          session.setStrategy(strategy.id, strategy.name)
          run.mutate(toBacktestRequest(backtest, strategy.id, form.timeframe), {
            onSuccess: (created) => {
              void navigate(`/results/${created.id}`)
            },
          })
        },
      },
    )
  }

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-semibold">Run a backtest</h2>

      <section className={sectionClass}>
        <label className="flex flex-col gap-1 text-sm">
          Strategy
          <select
            aria-label="strategy"
            className={inputClass}
            value={choiceId}
            onChange={(event) => {
              // Choosing a strategy loads its form outright, defaults and all. It replaces what was
              // there rather than merging: the two document shapes have nothing in common to keep,
              // and a half-carried-over form is how a run ends up with a parameter nobody chose.
              // That includes the name, which is re-stamped here — picking a strategy is the act
              // that starts a new lineage, so it is the act that mints a new name.
              const next = event.target.value
              setChoiceId(next)
              setForm(strategyChoice(next).form(new Date()))
            }}
          >
            {GROUPS.map((group) => (
              <optgroup key={group} label={group}>
                {STRATEGY_CHOICES.filter((choice) => choice.group === group).map((choice) => (
                  <option key={choice.id} value={choice.id}>
                    {choice.label}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        {form.mode === 'setup' && (
          <div className="mt-4">
            <SetupFields
              setup={form.setup}
              onChange={(next) => {
                patch({ setup: next })
              }}
            />
          </div>
        )}
      </section>

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

      {form.mode === 'conditions' && (
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
      )}

      {form.mode === 'conditions' && (
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
      )}

      <section className={sectionClass}>
        <h3 className="mb-2 font-medium">Exit</h3>
        <div className="mb-3 flex flex-wrap gap-4">
          {/* A setup places its own stop from the bar it entered on, and the semantic layer
              refuses a setup document that carries one — so the field is not offered. */}
          {form.mode === 'conditions' && (
            <>
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
        {form.mode === 'conditions' && (
          <ConditionRows
            label="Exit conditions"
            side={form.exit}
            onChange={(next) => {
              patch({ exit: next })
            }}
          />
        )}
      </section>

      <section className={sectionClass}>
        <h3 className="mb-3 font-medium">Where and when</h3>
        <BacktestSettings form={backtest} instruments={instruments.data} onChange={setBacktest} />
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

      {validation.valid && blocked !== null && (
        // Why the button is disabled, rather than leaving the user to guess which field is at fault.
        <p className="text-sm text-amber-300">Before running: {blocked}.</p>
      )}

      {save.isError && (
        <p className="text-sm text-red-400">
          The API rejected the strategy: {save.error.message}. A saved strategy is immutable for its
          version, and the name is what identifies the lineage — pick the strategy again to stamp a
          fresh name, or edit the name field by hand.
        </p>
      )}
      {run.isError && (
        <p className="text-sm text-red-400">
          The strategy was saved, but the backtest could not be enqueued: {run.error.message}
        </p>
      )}

      <button
        type="button"
        disabled={!validation.valid || blocked !== null || save.isPending || run.isPending}
        onClick={launch}
        className="rounded bg-sky-600 px-4 py-2 font-medium text-white enabled:hover:bg-sky-500 disabled:opacity-40"
      >
        {save.isPending || run.isPending ? 'Starting…' : 'Run backtest'}
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
        {INDICATOR_KINDS.map((kind) => (
          <option key={kind} value={kind}>
            {kind}
          </option>
        ))}
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
