import { TAKE_PROFIT_RR, setupSpec, type SchemaParam, type SetupType } from '@tradeforge/schema'
import { useState } from 'react'

import { useEditLiveSetup, useStrategy } from '../api/hooks'
import type { LiveSetup } from '../api/types'
import { ParamField } from './ParamField'

type Params = Record<string, unknown>

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
