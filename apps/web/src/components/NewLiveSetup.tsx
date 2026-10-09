import {
  SETUP_TYPES,
  TAKE_PROFIT_RR,
  setupSpec,
  type SchemaParam,
  type SetupType,
} from '@tradeforge/schema'
import { useState } from 'react'

import { useInstruments, useRegisterLiveSetup } from '../api/hooks'
import { TIMEFRAMES } from '../strategy/builder'
import { BrokerTag } from './BrokerTag'
import { switchedOn } from '../strategy/switchedOn'
import { ParamField } from './ParamField'

type Params = Record<string, unknown>

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/** Each parameter at its schema default; a required list with none starts at its first option. */
function defaultsOf(params: readonly SchemaParam[]): Params {
  const values: Params = {}
  for (const param of params) {
    if (param.kind === 'boolean') values[param.name] = param.default
    else if (param.kind === 'enum') {
      values[param.name] = param.default ?? (param.nullable ? null : (param.options[0] ?? null))
    } else values[param.name] = param.default ?? (param.nullable ? null : switchedOn(param))
  }
  return values
}

/**
 * Register a setup from scratch (09/10): a named setup with its parameters, one chart, the
 * markets to follow — and start following them. The strategy is saved and the setup made in one
 * request, so a refusal leaves nothing behind.
 */
export function NewLiveSetup(props: { onClose: () => void }): React.JSX.Element {
  const [type, setType] = useState<SetupType>(SETUP_TYPES[0] ?? 'structure_choch')
  const spec = setupSpec(type)
  const [name, setName] = useState('')
  const [timeframe, setTimeframe] = useState('H1')
  const [params, setParams] = useState<Params>(() => defaultsOf(spec.params))
  const [rr, setRr] = useState<number | null>(2)
  const [percent, setPercent] = useState(1)
  const [noTargetR, setNoTargetR] = useState(5)
  const [markets, setMarkets] = useState<ReadonlySet<string>>(new Set())
  const [filter, setFilter] = useState('')
  const instruments = useInstruments()
  const register = useRegisterLiveSetup()

  const shown = (instruments.data ?? []).filter((one) =>
    one.symbol.toLowerCase().includes(filter.trim().toLowerCase()),
  )
  const ready = name.trim() !== '' && markets.size > 0

  const save = (): void => {
    register.mutate(
      {
        definition: {
          schema_version: '1.0',
          name: name.trim(),
          timeframe,
          setup: { type, params },
          exit: {
            take_profit: rr === null ? null : { type: 'risk_multiple', params: { rr } },
          },
          risk: { sizing: { type: 'percent_risk', params: { percent } } },
        },
        timeframe,
        instrument_ids: [...markets],
        no_target_r: String(noTargetR),
      },
      {
        onSuccess: () => {
          props.onClose()
        },
      },
    )
  }

  return (
    <form
      className="space-y-4 rounded-lg border border-sky-900 p-4"
      onSubmit={(event) => {
        event.preventDefault()
        save()
      }}
    >
      <h2 className="text-base font-semibold text-slate-100">New setup</h2>
      <div className="flex flex-wrap gap-4">
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Name
          <input
            value={name}
            onChange={(event) => {
              setName(event.target.value)
            }}
            className={`${inputClass} w-64`}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Setup
          <select
            value={type}
            onChange={(event) => {
              const next = event.target.value as SetupType
              setType(next)
              setParams(defaultsOf(setupSpec(next).params))
            }}
            className={inputClass}
          >
            {SETUP_TYPES.map((one) => (
              <option key={one} value={one}>
                {one}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Chart
          <select
            value={timeframe}
            onChange={(event) => {
              setTimeframe(event.target.value)
            }}
            className={inputClass}
          >
            {TIMEFRAMES.map((one) => (
              <option key={one} value={one}>
                {one}
              </option>
            ))}
          </select>
        </label>
      </div>

      <fieldset className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
        <legend className="mb-1 text-xs text-slate-500">Parameters</legend>
        {spec.params.map((param) => (
          <ParamField
            key={`${type}-${param.name}`}
            param={param}
            value={params[param.name]}
            onChange={(value) => {
              setParams({ ...params, [param.name]: value })
            }}
          />
        ))}
        <ParamField
          param={{ ...TAKE_PROFIT_RR, name: 'take profit (R)' }}
          value={rr}
          onChange={(value) => {
            setRr(typeof value === 'number' ? value : null)
          }}
        />
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          risk per trade (%)
          <input
            type="number"
            step="any"
            min={0.01}
            value={percent}
            onChange={(event) => {
              setPercent(Number(event.target.value))
            }}
            className={`${inputClass} w-28`}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          no target: close at (R)
          <input
            type="number"
            step="any"
            min={0.1}
            value={noTargetR}
            onChange={(event) => {
              setNoTargetR(Number(event.target.value))
            }}
            className={`${inputClass} w-28`}
          />
        </label>
      </fieldset>

      <fieldset className="space-y-2">
        <legend className="text-xs text-slate-500">
          Markets to follow ({String(markets.size)} chosen)
        </legend>
        <input
          placeholder="filter…"
          aria-label="Filter markets"
          value={filter}
          onChange={(event) => {
            setFilter(event.target.value)
          }}
          className={`${inputClass} w-48`}
        />
        <div className="flex max-h-48 flex-wrap gap-2 overflow-y-auto">
          {shown.map((one) => (
            <label
              key={one.id}
              className="flex items-center gap-1 rounded border border-slate-800 px-2 py-0.5 text-sm"
            >
              <input
                type="checkbox"
                checked={markets.has(one.id)}
                onChange={() => {
                  const next = new Set(markets)
                  if (next.has(one.id)) next.delete(one.id)
                  else next.add(one.id)
                  setMarkets(next)
                }}
                className="size-3 accent-sky-500"
              />
              <span className="font-mono">{one.symbol}</span>
              <BrokerTag broker={one.broker} />
            </label>
          ))}
        </div>
      </fieldset>

      {register.error && <p className="text-sm text-red-400">{register.error.message}</p>}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={!ready || register.isPending}
          className="rounded bg-emerald-700 px-3 py-1 text-sm text-white hover:bg-emerald-600 disabled:opacity-50"
        >
          {register.isPending ? 'Starting…' : 'Start live'}
        </button>
        <button
          type="button"
          onClick={props.onClose}
          className="rounded border border-slate-700 px-3 py-1 text-sm hover:bg-slate-800"
        >
          Cancel
        </button>
      </div>
    </form>
  )
}
