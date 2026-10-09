import {
  TAKE_PROFIT_RR,
  offIsASetting,
  setupSpec,
  type SchemaParam,
  type SetupType,
} from '@tradeforge/schema'
import { useState } from 'react'

import { useEditLiveSetup, useStrategy } from '../api/hooks'
import type { LiveSetup } from '../api/types'

type Params = Record<string, unknown>

const inputClass =
  'w-28 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/**
 * Edit a followed setup's parameters (09/10). Saving makes a **new version** of its strategy —
 * the signals already posted keep theirs — and the setup's sessions restart on it.
 *
 * ⚠️ The fields are read from the schema (`setupSpec`), never listed here: a parameter added to a
 * setup in Python appears in this form with no code changed, with its own bounds and its own "off".
 */
export function SetupEditor(props: { setup: LiveSetup; onClose: () => void }): React.JSX.Element {
  const strategy = useStrategy(props.setup.strategy_id)
  const definition = strategy.data?.definition
  if (definition === undefined) {
    return <p className="text-sm text-slate-500">Loading the setup…</p>
  }
  const setup = definition.setup as { type?: string; params?: Params } | undefined
  let spec
  try {
    spec = setup?.type === undefined ? undefined : setupSpec(setup.type as SetupType)
  } catch {
    spec = undefined
  }
  if (setup === undefined || spec === undefined) {
    return (
      <p className="text-sm text-slate-400">
        Only a named setup can be edited here; this strategy is built from conditions — edit it in
        the catalogue.
      </p>
    )
  }
  return (
    <EditorForm
      setupId={props.setup.id}
      definition={definition}
      params={setup.params ?? {}}
      specParams={spec.params}
      onClose={props.onClose}
    />
  )
}

function rrOf(definition: Params): number | null {
  const exit = definition.exit as { take_profit?: { params?: { rr?: number } } | null } | undefined
  return exit?.take_profit?.params?.rr ?? null
}

function EditorForm(props: {
  setupId: string
  definition: Params
  params: Params
  specParams: readonly SchemaParam[]
  onClose: () => void
}): React.JSX.Element {
  const [params, setParams] = useState<Params>(props.params)
  const [rr, setRr] = useState<number | null>(rrOf(props.definition))
  const edit = useEditLiveSetup()

  const save = (): void => {
    const exit = (props.definition.exit ?? {}) as Params
    const definition = {
      ...props.definition,
      setup: { ...(props.definition.setup as Params), params },
      exit: {
        ...exit,
        take_profit: rr === null ? null : { type: 'risk_multiple', params: { rr } },
      },
    }
    edit.mutate(
      { id: props.setupId, definition },
      {
        onSuccess: () => {
          props.onClose()
        },
      },
    )
  }

  return (
    <form
      className="space-y-3 rounded border border-slate-800 p-3"
      onSubmit={(event) => {
        event.preventDefault()
        save()
      }}
    >
      <p className="text-xs text-slate-400">
        Saving makes a new version of this setup; its signals so far keep the version they came
        from, and its markets restart on the new one.
      </p>
      <div className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
        {props.specParams.map((param) => (
          <ParamField
            key={param.name}
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
      </div>
      {edit.error && <p className="text-sm text-red-400">{edit.error.message}</p>}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={edit.isPending}
          className="rounded bg-sky-700 px-3 py-1 text-sm text-white hover:bg-sky-600 disabled:opacity-50"
        >
          {edit.isPending ? 'Saving…' : 'Save as a new version'}
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

function ParamField(props: {
  param: SchemaParam
  value: unknown
  onChange: (value: unknown) => void
}): React.JSX.Element {
  const { param, value, onChange } = props
  const id = `param-${param.name}`
  const label = (
    <label htmlFor={id} className="text-xs text-slate-400">
      {param.name}
    </label>
  )

  if (param.kind === 'boolean') {
    return (
      <div className="flex items-center gap-2">
        <input
          id={id}
          type="checkbox"
          checked={value === true}
          onChange={(event) => {
            onChange(event.target.checked)
          }}
          className="size-4 accent-sky-500"
        />
        {label}
      </div>
    )
  }

  const canBeOff = offIsASetting(param)

  if (param.kind === 'enum') {
    return (
      <div className="flex flex-col gap-1">
        {label}
        <select
          id={id}
          value={typeof value === 'string' ? value : ''}
          onChange={(event) => {
            onChange(event.target.value === '' ? null : event.target.value)
          }}
          className={inputClass}
        >
          {(canBeOff || value === null || value === undefined) && (
            <option value="">{canBeOff ? 'off' : '—'}</option>
          )}
          {param.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </div>
    )
  }

  const off = value === null || value === undefined
  return (
    <div className="flex flex-col gap-1">
      {label}
      <span className="flex items-center gap-2">
        <input
          id={id}
          type="number"
          step={param.kind === 'integer' ? 1 : 'any'}
          min={param.min}
          max={param.max}
          disabled={off && canBeOff}
          value={typeof value === 'number' ? value : ''}
          onChange={(event) => {
            onChange(event.target.value === '' ? null : Number(event.target.value))
          }}
          className={`${inputClass} disabled:opacity-40`}
        />
        {canBeOff && (
          <label className="flex items-center gap-1 text-xs text-slate-400">
            <input
              type="checkbox"
              checked={off}
              aria-label={`${param.name} off`}
              onChange={(event) => {
                onChange(event.target.checked ? null : switchedOn(param))
              }}
              className="size-3 accent-sky-500"
            />
            off
          </label>
        )}
      </span>
    </div>
  )
}

/**
 * The value a parameter takes when switched back on: its default, else its lowest legal value —
 * one past an exclusive bound, since `rr > 0` refuses the 0 the schema names as its minimum.
 */
function switchedOn(param: SchemaParam): number {
  if (param.kind !== 'integer' && param.kind !== 'number') return 1
  if (param.default !== null) return param.default
  if (param.min === undefined) return 1
  return param.minExclusive === true ? param.min + 1 : param.min
}
