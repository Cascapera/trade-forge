import {
  indicatorSpec,
  SETUPS,
  setupSpec,
  takesSource,
  validateStrategy,
  type SchemaParam,
  type SetupType,
} from '@tradeforge/schema'
import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { useCreateBacktest, useInstruments, useSaveStrategy, useStrategy } from '../api/hooks'
import { emptyBacktestForm, toBacktestRequest, whyNotRunnable, type BacktestForm } from '../backtest/settings'
import { BacktestSettings } from '../components/BacktestSettings'
import { NumberStepper } from '../components/NumberStepper'
import { useSession } from '../store'
import {
  buildStrategy,
  catalogueHas,
  CANDLE_REF,
  CANDLE_REF_SEED,
  candleRef,
  emptyGroup,
  emptyRow,
  INDICATOR_KINDS,
  indicatorValues,
  OP_GROUPS,
  opOf,
  setupValues,
  STRATEGY_CHOICES,
  parseCandleRef,
  refCatalogue,
  retypedValues,
  SOURCES,
  strategyChoice,
  subjectName,
  subjectOf,
  TIMEFRAMES,
  withOp,
  withSubject,
  type Combine,
  type ConditionNode,
  type ConditionRow,
  type IndicatorForm,
  type OperandForm,
  type RefGroup,
  type RowOp,
  type Source,
  type SetupForm,
  type SideForm,
  type StrategyChoice,
  type StrategyForm,
} from '../strategy/builder'
import { formOf, type ParseResult } from '../strategy/parse'

/** The headings the picker groups by, in the order they are offered. */
const GROUPS: readonly StrategyChoice['group'][] = ['Setups', 'Conditions']

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'
const sectionClass = 'rounded-lg border border-slate-800 bg-slate-900/40 p-4'

/**
 * The name of something the engine can resolve, chosen rather than typed.
 *
 * ⚠️ **The box does not go away, and that is deliberate.** Three of the grammar's four ref forms
 * are enumerable; `candle[-N].field` is not, because N is unbounded. So the list carries a
 * `custom` entry that seeds a well-formed candle ref and reveals the box — a picker without one
 * would make that form unreachable from the screen, which is trading one unreachable corner of
 * the grammar for another.
 *
 * Whether the box shows is **derived** from the value, never stored: anything the catalogue
 * cannot offer is being written by hand, including a ref left dangling by an indicator that was
 * renamed. Storing a flag instead would let it disagree with the value it describes.
 */
function RefPicker(props: {
  label: string
  value: string
  groups: readonly RefGroup[]
  onChange: (next: string) => void
}): React.JSX.Element {
  const { label, value, groups, onChange } = props
  const candle = parseCandleRef(value)
  const known = catalogueHas(groups, value)
  // Neither offerable nor a closed-candle ref: a name somebody typed, or one left dangling by an
  // indicator that was renamed. Derived, never stored — a stored flag could disagree with the
  // value it describes, and the renamed case is exactly when it would.
  const stale = value !== '' && !known && candle === null
  return (
    <>
      <select
        aria-label={label}
        className={inputClass}
        value={candle === null ? value : CANDLE_REF}
        onChange={(event) => {
          onChange(event.target.value === CANDLE_REF ? CANDLE_REF_SEED : event.target.value)
        }}
      >
        <option value="">choose…</option>
        {groups.map((group) => (
          <optgroup key={group.label} label={group.label}>
            {group.refs.map((ref) => (
              <option key={ref} value={ref}>
                {ref}
              </option>
            ))}
          </optgroup>
        ))}
        <optgroup label="a closed candle">
          <option value={CANDLE_REF}>candle[-N].field</option>
        </optgroup>
        {stale && <option value={value}>{value}</option>}
      </select>
      {candle !== null && (
        <>
          <input
            aria-label={`${label} bars back`}
            className={inputClass}
            type="number"
            min={1}
            value={candle.bars}
            onChange={(event) => {
              onChange(candleRef(event.target.value, candle.field))
            }}
          />
          <span className="text-sm text-slate-500">bars back</span>
          <select
            aria-label={`${label} field`}
            className={inputClass}
            value={candle.field}
            onChange={(event) => {
              onChange(candleRef(candle.bars, event.target.value as Source))
            }}
          >
            {SOURCES.map((source) => (
              <option key={source} value={source}>
                {source}
              </option>
            ))}
          </select>
        </>
      )}
    </>
  )
}

/**
 * An operand box and the one-word question that says how to read it: a name the engine resolves
 * or a number typed literally.
 *
 * Shared by every bounded operand — the right-hand side of a comparison and both edges of a band
 * — because they are the same widget. The subject of a row is deliberately *not* one of these:
 * asking whether `rsi` is inside `[30, 70]` is a question about a series, and a literal there
 * would be a band drawn around a constant.
 */
function OperandInput(props: {
  label: string
  value: OperandForm
  groups: readonly RefGroup[]
  onChange: (next: OperandForm) => void
}): React.JSX.Element {
  const { label, value, groups, onChange } = props
  return (
    <>
      <select
        aria-label={`${label} kind`}
        className={inputClass}
        value={value.kind}
        onChange={(event) => {
          onChange({ ...value, kind: event.target.value as OperandForm['kind'] })
        }}
      >
        <option value="ref">ref</option>
        <option value="value">value</option>
      </select>
      {value.kind === 'ref' ? (
        <RefPicker
          label={label}
          value={value.text}
          groups={groups}
          onChange={(text) => {
            onChange({ ...value, text })
          }}
        />
      ) : (
        <input
          aria-label={label}
          className={inputClass}
          type="number"
          placeholder="30"
          value={value.text}
          onChange={(event) => {
            onChange({ ...value, text: event.target.value })
          }}
        />
      )}
    </>
  )
}

/**
 * The fields of one leaf: its subject, its operator, and whatever operands that operator takes.
 *
 * `path` names the node's place in the tree and is what every control answers to — `0` at the top
 * level, `1.0` for the first child of the second row. Top-level names are unchanged from when the
 * list was flat, which is not an accident: a rename would have quietly rewritten every existing
 * test's idea of what it was clicking.
 */
function RowFields(props: {
  label: string
  path: string
  row: ConditionRow
  groups: readonly RefGroup[]
  onChange: (next: ConditionRow) => void
}): React.JSX.Element {
  const { label, path, row, groups, onChange } = props
  return (
    <>
      <RefPicker
        label={`${label} ${subjectName(row.shape)} ${path}`}
        value={subjectOf(row)}
        groups={groups}
        onChange={(chosen) => {
          onChange(withSubject(row, chosen))
        }}
      />
      <select
        aria-label={`${label} op ${path}`}
        className={inputClass}
        value={opOf(row)}
        onChange={(event) => {
          onChange(withOp(row, event.target.value as RowOp))
        }}
      >
        {OP_GROUPS.map((group) => (
          <optgroup key={group.shape} label={group.label}>
            {group.ops.map((op) => (
              <option key={op} value={op}>
                {op}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
      {row.shape === 'comparison' && (
        <OperandInput
          label={`${label} right ${path}`}
          value={row.right}
          groups={groups}
          onChange={(next) => {
            onChange({ ...row, right: next })
          }}
        />
      )}
      {row.shape === 'between' && (
        <>
          <OperandInput
            label={`${label} low ${path}`}
            value={row.low}
            groups={groups}
            onChange={(next) => {
              onChange({ ...row, low: next })
            }}
          />
          <span className="text-sm text-slate-500">and</span>
          <OperandInput
            label={`${label} high ${path}`}
            value={row.high}
            groups={groups}
            onChange={(next) => {
              onChange({ ...row, high: next })
            }}
          />
        </>
      )}
      {row.shape === 'trend' && (
        <>
          <span className="text-sm text-slate-500">for</span>
          <input
            aria-label={`${label} bars ${path}`}
            className={inputClass}
            type="number"
            min={1}
            // Empty is not 1 typed out: it leaves the key off the node, so the engine's own
            // default applies. The placeholder says which number that is.
            placeholder="1"
            value={row.bars}
            onChange={(event) => {
              onChange({ ...row, bars: event.target.value })
            }}
          />
          <span className="text-sm text-slate-500">bars</span>
        </>
      )}
    </>
  )
}

/**
 * The two things that can be added anywhere a condition can go.
 *
 * ⚠️ Every accessible name here **contains its visible text** (`Long + condition 1`, not
 * `Long add condition 1`). The label has to disambiguate — there are now as many `+ condition`
 * buttons as there are groups — and a name that replaces what the reader can see is the other
 * half of the same a11y defect it is fixing.
 */
function AddButtons(props: {
  label: string
  path: string
  onAdd: (node: ConditionNode) => void
}): React.JSX.Element {
  const { label, path, onAdd } = props
  const at = path === '' ? '' : ` ${path}`
  return (
    <div className="flex gap-3">
      <button
        type="button"
        aria-label={`${label} + condition${at}`}
        className="text-sm text-sky-400 hover:text-sky-300"
        onClick={() => {
          onAdd(emptyRow())
        }}
      >
        + condition
      </button>
      <button
        type="button"
        aria-label={`${label} + group${at}`}
        className="text-sm text-sky-400 hover:text-sky-300"
        onClick={() => {
          onAdd(emptyGroup())
        }}
      >
        + group
      </button>
    </div>
  )
}

/**
 * One node of the tree, whatever shape it is — the recursion that makes nesting possible.
 *
 * ⚠️ **`not` is drawn as a wrapper around its child rather than as a node with an editor of its
 * own.** It has exactly one child and nothing to configure, so giving it a box with its own
 * remove button would ask the reader to tell "remove the negation" from "remove the rule inside
 * it" on every single one. The button toggles the wrapper; remove removes what is underneath.
 */
function NodeEditor(props: {
  label: string
  path: string
  node: ConditionNode
  groups: readonly RefGroup[]
  onChange: (next: ConditionNode) => void
  onRemove: () => void
}): React.JSX.Element {
  const { label, path, node, groups, onChange, onRemove } = props

  if (node.shape === 'not') {
    return (
      <div className="flex items-start gap-2 rounded border border-rose-900/60 bg-rose-950/20 p-2">
        <span className="mt-1 text-xs font-semibold tracking-wide text-rose-300">NOT</span>
        <div className="flex-1">
          <NodeEditor
            label={label}
            path={path}
            node={node.child}
            groups={groups}
            onChange={(next) => {
              onChange({ shape: 'not', child: next })
            }}
            onRemove={onRemove}
          />
        </div>
        <button
          type="button"
          aria-label={`${label} un-not ${path}`}
          className="text-xs text-rose-300 hover:text-rose-200"
          onClick={() => {
            onChange(node.child)
          }}
        >
          un-not
        </button>
      </div>
    )
  }

  const negate = (
    <button
      type="button"
      aria-label={`${label} not ${path}`}
      className="text-xs text-slate-500 hover:text-rose-300"
      onClick={() => {
        onChange({ shape: 'not', child: node })
      }}
    >
      not
    </button>
  )
  const remove = (
    <button
      type="button"
      aria-label={`${label} remove ${path}`}
      className="text-slate-500 hover:text-red-400"
      onClick={onRemove}
    >
      remove
    </button>
  )

  if (node.shape === 'group') {
    return (
      <div className="space-y-2 rounded border border-slate-700 bg-slate-900/40 p-2">
        <div className="flex items-center gap-2">
          <select
            aria-label={`${label} combine ${path}`}
            className={inputClass}
            value={node.combine}
            onChange={(event) => {
              onChange({ ...node, combine: event.target.value as Combine })
            }}
          >
            <option value="all">match all</option>
            <option value="any">match any</option>
          </select>
          {negate}
          {remove}
        </div>
        {node.children.map((child, index) => (
          <NodeEditor
            key={index}
            label={label}
            path={`${path}.${String(index)}`}
            node={child}
            groups={groups}
            onChange={(next) => {
              onChange({
                ...node,
                children: node.children.map((one, i) => (i === index ? next : one)),
              })
            }}
            onRemove={() => {
              // ⚠️ A group whose last child is removed goes with it. The alternative is a group
              // with no children, which the schema refuses (`all` takes a non-empty list) and
              // which would turn one click into a document nobody can save.
              const rest = node.children.filter((_, i) => i !== index)
              if (rest.length === 0) onRemove()
              else onChange({ ...node, children: rest })
            }}
          />
        ))}
        <AddButtons
          label={label}
          path={path}
          onAdd={(added) => {
            onChange({ ...node, children: [...node.children, added] })
          }}
        />
      </div>
    )
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <RowFields label={label} path={path} row={node} groups={groups} onChange={onChange} />
      {negate}
      {remove}
    </div>
  )
}

function ConditionRows(props: {
  label: string
  side: SideForm
  /** The refs this strategy's own indicators make available — the picker's second group. */
  groups: readonly RefGroup[]
  onChange: (next: SideForm) => void
}): React.JSX.Element {
  const { label, side, groups, onChange } = props
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
          <NodeEditor
            key={index}
            label={label}
            path={String(index)}
            node={row}
            groups={groups}
            onChange={(next) => {
              onChange({ ...side, rows: side.rows.map((one, i) => (i === index ? next : one)) })
            }}
            onRemove={() => {
              onChange({ ...side, rows: side.rows.filter((_, i) => i !== index) })
            }}
          />
        ))}
      {side.enabled && (
        <AddButtons
          label={label}
          path=""
          onAdd={(added) => {
            onChange({ ...side, rows: [...side.rows, added] })
          }}
        />
      )}
    </div>
  )
}

/** What an empty box means for this parameter, said out loud — the distinction is invisible
 *  otherwise, and it is the difference between two different experiments. */
function emptyHint(param: SchemaParam): string | null {
  if (param.kind === 'boolean' || !('nullable' in param) || !param.nullable) return null
  return param.name === 'max_bos' ? 'empty = uncapped' : 'empty = off'
}

/**
 * One parameter, rendered as whatever control its schema node calls for.
 *
 * ⚠️ **The prefix is a parameter because this renders indicator fields too.** It used to be
 * hard-coded `setup`, and that was the visible half of a deeper problem: the indicator form was a
 * separate, hand-written set of fields, so a parameter the DSL gained reached one form and not
 * the other. `deviations` is what that cost — valid in the schema, unreachable from the screen.
 */
function ParamField(props: {
  param: SchemaParam
  /** What the control answers to: `setup side`, `indicator deviations`. */
  prefix: string
  value: string | boolean | undefined
  onChange: (next: string | boolean) => void
}): React.JSX.Element {
  const { param, prefix, value, onChange } = props
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
          aria-label={`${prefix} ${param.name}`}
          type="checkbox"
          className="self-start"
          checked={value === true}
          onChange={(event) => {
            onChange(event.target.checked)
          }}
        />
      ) : param.kind === 'enum' ? (
        <select
          aria-label={`${prefix} ${param.name}`}
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
          label={`${prefix} ${param.name}`}
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
          <ParamField
            key={param.name}
            param={param}
            prefix="setup"
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
  const { id: openedId } = useParams<{ id: string }>()
  const opened = useStrategy(openedId)
  const navigate = useNavigate()
  const session = useSession()
  const instruments = useInstruments()
  const save = useSaveStrategy()
  const run = useCreateBacktest()

  /**
   * A saved strategy, read back into the form — or the reasons it cannot be shown.
   *
   * ⚠️ **Derived from the fetched document, not copied into state by an effect.** An effect that
   * called `setForm` would fight every keystroke the moment the query refetched, and the bug it
   * produces is edits vanishing under the reader's hands. `loaded` is the parse; the form state
   * below adopts it once, keyed by the document itself.
   */
  const loaded = useMemo((): ParseResult | null => {
    if (opened.data === undefined) return null
    // ⚠️ Validated before it is parsed, rather than asserted into shape. The API only stores
    // documents it validated — but under *its* schema, and a strategy saved by an older one is
    // exactly the case where a cast would hand `formOf` a shape it reasons about wrongly and
    // produce a form that looks fine. A refusal naming the field is the outcome worth having.
    const checked = validateStrategy(opened.data.definition)
    if (!checked.valid) {
      return {
        ok: false,
        unsupported: checked.errors.map((error) => `${error.path}: ${error.message}`),
      }
    }
    return formOf(checked.strategy)
  }, [opened.data])
  // Adopting the parsed form is a render-phase state update keyed on the document — React's own
  // idiom for "derive state from props" — so typing is never overwritten by a refetch.
  //
  // ⚠️ **Keyed on the fetched object, not on the id from the route.** Keying on the id looks
  // equivalent and loops forever the moment the id is absent: `adopted` would be set to `null`,
  // which never equals `undefined`, so the condition stays true on every render. React caught it
  // as "Too many re-renders"; the object reference is stable while the query result is, and it is
  // what "the document was adopted" actually means.
  const [adopted, setAdopted] = useState<unknown>(null)
  if (loaded?.ok === true && adopted !== opened.data) {
    setAdopted(opened.data)
    setForm(loaded.form)
  }

  // Recomputed whenever the indicator list changes: declaring an indicator has to make its name
  // offerable in the same keystroke, and renaming one has to stop offering the old spelling.
  const refs = useMemo(() => refCatalogue(form.indicators), [form.indicators])
  const document = useMemo(() => buildStrategy(form), [form])
  const validation = useMemo(() => validateStrategy(document), [document])
  const blocked = whyNotRunnable(backtest, instruments.data)

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

      {/* ⚠️ Said out loud, and the form below is left as it was. The tempting alternative is to
          show the parts that could be read and let the reader carry on — which hands them a form
          that looks complete and writes a different strategy over their own the moment they save.
          Refusing is the safe half of that trade, and naming the paths is what makes it useful. */}
      {loaded?.ok === false && (
        <div className="rounded border border-amber-700 bg-amber-950/40 p-3 text-sm">
          <p className="mb-2 font-medium text-amber-300">
            This strategy cannot be opened in the builder yet:
          </p>
          <ul className="list-inside list-disc text-amber-200">
            {loaded.unsupported.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}

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
                    { id: '', kind: 'SMA', values: indicatorValues('SMA') },
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
              groups={refs}
              onChange={(next) => {
                patch({ long: next })
              }}
            />
            <ConditionRows
              label="Short"
              side={form.short}
              groups={refs}
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
            groups={refs}
            onChange={(next) => {
              patch({ exit: next })
            }}
          />
        )}
      </section>

      <section className={sectionClass}>
        <h3 className="mb-3 font-medium">Where and when</h3>
        {/* The timeframe belongs to the strategy document, so the builder reads it from the
            form it is editing rather than asking for it a second time. */}
        <BacktestSettings
          form={backtest}
          instruments={instruments.data}
          onChange={setBacktest}
          timeframe={form.timeframe}
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
  // Straight off the schema, exactly as the setup form reads its own: kind, bounds, nullability
  // and default, required first. An indicator that gains a parameter in Python gains a control
  // here once the types are regenerated, with no edit to this file — which is the whole reason
  // `deviations` was unreachable before and is not now.
  const spec = indicatorSpec(indicator.kind)
  return (
    <div className="flex flex-wrap items-end gap-2">
      <label className="flex flex-col gap-1 text-sm">
        <span>id</span>
        <input
          aria-label="indicator id"
          className={inputClass}
          placeholder="id"
          value={indicator.id}
          onChange={(event) => {
            onChange({ ...indicator, id: event.target.value })
          }}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        <span>kind</span>
        <select
          aria-label="indicator kind"
          className={inputClass}
          value={indicator.kind}
          onChange={(event) => {
            // ⚠️ Values carry over where the new kind has a place for them — unlike the setup
            // picker, which starts from scratch. `period` is a window on all eight indicators;
            // making somebody retype 20 to compare an SMA(20) with an EMA(20) is how the
            // comparison stops being made. See `retypedValues` for why setups differ.
            const kind = event.target.value as IndicatorForm['kind']
            onChange({ ...indicator, kind, values: retypedValues(kind, indicator.values) })
          }}
        >
          {INDICATOR_KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {kind}
            </option>
          ))}
        </select>
      </label>
      {spec.params.map((param) => (
        <ParamField
          key={param.name}
          param={param}
          prefix="indicator"
          value={indicator.values[param.name]}
          onChange={(next) => {
            onChange({ ...indicator, values: { ...indicator.values, [param.name]: next } })
          }}
        />
      ))}
      {/* ⚠️ Said out loud, because its absence is the interesting part. ATR and the two channels
          are defined over the whole candle, so "the ATR of the close" is not a setting they have
          — and a form that simply showed nothing there reads as a form that forgot. */}
      {!takesSource(indicator.kind) && (
        <span className="self-center text-xs text-slate-500">whole candle</span>
      )}
      <button type="button" className="self-center text-slate-500 hover:text-red-400" onClick={onRemove}>
        remove
      </button>
    </div>
  )
}
