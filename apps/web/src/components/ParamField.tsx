import { offIsASetting, type SchemaParam } from '@tradeforge/schema'

import { switchedOn } from '../strategy/switchedOn'

const inputClass =
  'w-28 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/**
 * One parameter of a setup, as the schema describes it (09/10): a menu for a list, a box for a
 * switch, a bounded number — and "off" only where the schema lets the value be switched off.
 */
export function ParamField(props: {
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
