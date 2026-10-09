import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { useBrowseSymbols, useMarkets } from '../api/hooks'
import type { BrowsedSymbol, Market } from '../api/types'
import { browsedCost } from '../basket/settings'
import { BrokerTag } from './BrokerTag'
import { Pager } from './Pager'
import { inputClass } from './symbolSearch'

/** Rows on one page of a market. */
export const BROWSE_PAGE = 25

/** What one request asks for when the whole market is chosen at once — the API's own ceiling. */
const WHOLE_MARKET = 500

const SEARCH_DEBOUNCE_MS = 250

/**
 * The token each market wears on its tab — letters rather than an icon set, so it renders the
 * same on every machine (Windows draws no flag emoji) and needs no dependency.
 */
const TOKENS: Record<string, { glyph: string; tone: string }> = {
  forex: { glyph: '€$', tone: 'bg-emerald-900/70 text-emerald-200' },
  crypto: { glyph: '₿', tone: 'bg-amber-900/70 text-amber-200' },
  indices: { glyph: '▲', tone: 'bg-sky-900/70 text-sky-200' },
  metals: { glyph: 'Au', tone: 'bg-yellow-900/70 text-yellow-200' },
  commodities: { glyph: 'Oil', tone: 'bg-orange-900/70 text-orange-200' },
  stocks_us: { glyph: 'US', tone: 'bg-blue-900/70 text-blue-200' },
  stocks_br: { glyph: 'BR', tone: 'bg-lime-900/70 text-lime-200' },
  stocks_other: { glyph: 'EQ', tone: 'bg-violet-900/70 text-violet-200' },
  futures: { glyph: 'Fut', tone: 'bg-slate-700 text-slate-200' },
}
const OTHER_TOKEN = { glyph: '…', tone: 'bg-slate-800 text-slate-300' }

export function MarketToken(props: { market: string }): React.JSX.Element {
  const token = TOKENS[props.market] ?? OTHER_TOKEN
  return (
    <span
      aria-hidden="true"
      className={`inline-flex h-6 min-w-6 items-center justify-center rounded-full px-1 text-[10px] font-semibold ${token.tone}`}
    >
      {token.glyph}
    </span>
  )
}

/** The first market worth opening: one with symbols the current filter shows. */
function firstMarket(markets: readonly Market[], collectedOnly: boolean): string {
  const shown = markets.find((one) => (collectedOnly ? one.collected : one.count) > 0)
  return (shown ?? markets[0])?.key ?? 'forex'
}

function useDebounced(text: string): string {
  const [debounced, setDebounced] = useState(text)
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebounced(text)
    }, SEARCH_DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
    }
  }, [text])
  return debounced
}

/**
 * The broker's list a market at a time, in a dialog (04/10, his ask: "200 to 500 assets, chosen
 * by market — crypto, forex, indices, US and Brazilian shares — paginated or in a modal").
 *
 * A tab per market, a search over the symbol and its description, 25 rows a page, and the two
 * choices a market-sized question needs: this page, or the whole market. The ticks are written
 * straight into the form, so closing the dialog is all "done" means.
 *
 * ⚠️ **Only collected markets by default.** A sweep runs on candles already on disk; the broker
 * lists hundreds the collector has never seen. The switch shows them — a launch refuses them
 * until they are collected — because finding one to collect is a reason to open this.
 *
 * ⚠️ At `max`, adding is refused and removing is not.
 */
export function MarketBrowser(props: {
  chosen: readonly string[]
  max: number
  onChange: (next: string[]) => void
  onClose: () => void
}): React.JSX.Element {
  const { chosen, max, onChange, onClose } = props
  const titleId = useId()
  const searchRef = useRef<HTMLInputElement>(null)
  const client = useQueryClient()

  const [collectedOnly, setCollectedOnly] = useState(true)
  const [picked, setPicked] = useState<string | null>(null)
  const [text, setText] = useState('')
  const [offset, setOffset] = useState(0)
  const [notice, setNotice] = useState<string | null>(null)
  const q = useDebounced(text.trim())

  const markets = useMarkets()
  const list = markets.data?.markets ?? []
  const market = picked ?? firstMarket(list, collectedOnly)
  const params = { market, q, collected: collectedOnly, offset, limit: BROWSE_PAGE }
  const page = useBrowseSymbols(params)

  useEffect(() => {
    searchRef.current?.focus()
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const chosenSet = new Set(chosen)
  const full = chosen.length >= max

  /** Add `symbols` in order, up to the ceiling, and say so when it cut. */
  const add = (symbols: readonly string[]): void => {
    const fresh = symbols.filter((symbol) => !chosenSet.has(symbol))
    const room = Math.max(0, max - chosen.length)
    if (fresh.length > room) {
      setNotice(
        `⚠️ ${String(max)} markets at most — ${String(fresh.length - room)} of these were left out.`,
      )
    } else {
      setNotice(null)
    }
    if (room > 0 && fresh.length > 0) onChange([...chosen, ...fresh.slice(0, room)])
  }

  const remove = (symbols: readonly string[]): void => {
    const gone = new Set(symbols)
    setNotice(null)
    onChange(chosen.filter((symbol) => !gone.has(symbol)))
  }

  /** Every symbol of the market under the current search and switch — not only this page. */
  const wholeMarket = async (): Promise<string[]> => {
    const whole = { market, q, collected: collectedOnly, offset: 0, limit: WHOLE_MARKET }
    const found = await client.query({
      queryKey: ['symbols', 'browse', whole],
      queryFn: () => api.browseSymbols(whole),
    })
    return found.items.map((one) => one.symbol)
  }

  const rows = page.data?.items ?? []
  const total = page.data?.total ?? 0
  const button =
    'rounded border border-slate-700 px-2 py-1 text-xs hover:border-slate-500 disabled:opacity-40'

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 sm:p-8"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="w-full max-w-4xl space-y-3 rounded-lg border border-slate-700 bg-slate-950 p-4 shadow-2xl"
      >
        <header className="flex items-center gap-3">
          <h2 id={titleId} className="text-lg font-semibold">
            Choose markets
          </h2>
          <span className="text-sm text-slate-400">
            {String(chosen.length)} of {String(max)} chosen
          </span>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded bg-sky-700 px-3 py-1 text-sm font-medium hover:bg-sky-600"
          >
            Done
          </button>
        </header>

        <div role="tablist" aria-label="Markets" className="flex flex-wrap gap-1">
          {list.map((one) => {
            const count = collectedOnly ? one.collected : one.count
            const active = one.key === market
            return (
              <button
                key={one.key}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => {
                  setPicked(one.key)
                  setOffset(0)
                  setNotice(null)
                }}
                className={`flex items-center gap-2 rounded-full border py-1 pr-3 pl-1 text-sm ${
                  active
                    ? 'border-sky-600 bg-sky-950/60 text-slate-100'
                    : 'border-slate-800 bg-slate-900/40 text-slate-300 hover:border-slate-600'
                } ${count === 0 && !active ? 'opacity-40' : ''}`}
              >
                <MarketToken market={one.key} />
                {one.label}
                <span className="text-xs text-slate-500 tabular-nums">{String(count)}</span>
              </button>
            )
          })}
          {markets.isPending && <p className="text-sm text-slate-400">Loading the markets…</p>}
        </div>

        <div className="flex flex-wrap items-end gap-3">
          <label className="flex grow flex-col gap-1 text-xs text-slate-400">
            Search this market
            <input
              ref={searchRef}
              type="search"
              className={inputClass}
              placeholder="ticker or name…"
              value={text}
              onChange={(event) => {
                setText(event.target.value)
                setOffset(0)
              }}
            />
          </label>
          <label className="flex items-center gap-2 pb-1 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={collectedOnly}
              onChange={() => {
                setCollectedOnly(!collectedOnly)
                setOffset(0)
              }}
              className="size-4 accent-sky-500"
            />
            Only collected
          </label>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className={button}
            disabled={rows.length === 0 || full}
            onClick={() => {
              add(rows.map((one) => one.symbol))
            }}
          >
            Choose this page
          </button>
          <button
            type="button"
            className={button}
            disabled={total === 0 || full}
            onClick={() => {
              void wholeMarket().then(add)
            }}
          >
            Choose all {String(total)}
          </button>
          <button
            type="button"
            className={button}
            disabled={total === 0 || chosen.length === 0}
            onClick={() => {
              void wholeMarket().then(remove)
            }}
          >
            Clear these
          </button>
          {notice !== null && <span className="text-xs text-amber-300">{notice}</span>}
        </div>

        <BrowseRows
          rows={rows}
          loading={page.isPending}
          neverSynced={markets.data?.snapshot === null}
          chosen={chosenSet}
          full={full}
          max={max}
          onToggle={(symbol) => {
            if (chosenSet.has(symbol)) remove([symbol])
            else add([symbol])
          }}
        />

        <Pager
          label="Symbol pages"
          offset={offset}
          limit={BROWSE_PAGE}
          total={total}
          onOffset={setOffset}
        />
      </div>
    </div>
  )
}

function BrowseRows(props: {
  rows: readonly BrowsedSymbol[]
  loading: boolean
  neverSynced: boolean
  chosen: ReadonlySet<string>
  full: boolean
  max: number
  onToggle: (symbol: string) => void
}): React.JSX.Element {
  const { rows, loading, neverSynced, chosen, full, max, onToggle } = props
  if (loading) return <p className="text-sm text-slate-400">Loading…</p>
  if (neverSynced) {
    return (
      <p className="text-sm text-slate-400">
        The broker's list was never synced —{' '}
        <Link to="/collect" className="underline hover:text-slate-200">
          sync it on the collect screen
        </Link>
        .
      </p>
    )
  }
  if (rows.length === 0) {
    return <p className="text-sm text-slate-400">Nothing here matches.</p>
  }
  return (
    <ul className="divide-y divide-slate-800 rounded border border-slate-800">
      {rows.map((row) => {
        const picked = chosen.has(row.symbol)
        const blocked = !picked && full
        const cost = browsedCost(row)
        return (
          <li key={`${row.broker ?? ''}:${row.ticker ?? row.symbol}`}>
            <label
              className={`flex items-center gap-3 px-3 py-2 text-sm ${
                picked ? 'bg-sky-950/40' : 'hover:bg-slate-900/60'
              } ${blocked ? 'opacity-40' : ''}`}
            >
              <input
                type="checkbox"
                checked={picked}
                disabled={blocked}
                onChange={() => {
                  onToggle(row.symbol)
                }}
                // The cost is part of the name: choosing by ear is the same decision as by eye.
                aria-label={`${row.symbol}, ${cost}`}
                title={blocked ? `Already at ${String(max)} markets — untick one first` : undefined}
                className="size-4 accent-sky-500 disabled:opacity-30"
              />
              <span className="w-28 shrink-0 font-mono font-medium">
                {row.symbol}
                {/* The broker's own ticker, when the system names it otherwise (WIN for WIN$). */}
                {row.ticker && row.ticker !== row.symbol && (
                  <span className="ml-1 text-xs font-normal text-slate-500">{row.ticker}</span>
                )}
              </span>
              <span className="hidden min-w-0 grow truncate text-slate-400 sm:block">
                {row.description}
              </span>
              <BrokerTag broker={row.broker} />
              <span
                className={`ml-auto shrink-0 text-xs ${
                  row.catalogued && row.spread_points !== null ? 'text-slate-400' : 'text-amber-300'
                }`}
              >
                {cost}
              </span>
            </label>
          </li>
        )
      })}
    </ul>
  )
}
