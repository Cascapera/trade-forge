import { NavLink, Navigate, Route, Routes } from 'react-router-dom'

import { BasketResult } from './screens/BasketResult'
import { CollectSymbol } from './screens/CollectSymbol'
import { LaunchBacktest } from './screens/LaunchBacktest'
import { LaunchBasket } from './screens/LaunchBasket'
import { Results } from './screens/Results'
import { LaunchStudy } from './screens/LaunchStudy'
import { LiveSessions } from './screens/LiveSessions'
import { RunLog } from './screens/RunLog'
import { StrategyBuilder } from './screens/StrategyBuilder'
import { StrategyCatalog } from './screens/StrategyCatalog'
import { StudyResult } from './screens/StudyResult'
import { WalkForwardResult } from './screens/WalkForwardResult'
import { useSession } from './store'

function navClass({ isActive }: { isActive: boolean }): string {
  const base = 'block rounded px-3 py-1.5 text-sm transition-colors'
  return isActive
    ? `${base} bg-slate-800 font-semibold text-sky-400`
    : `${base} text-slate-400 hover:bg-slate-900 hover:text-slate-200`
}

/**
 * A titled group of links.
 *
 * The sidebar exists because the top bar had run out of room: seven links plus up to three session
 * threads on one line, and the reader had to read all ten to find the one they wanted. A column
 * has room for headings, and a heading is what turns a list of names into a place — the question
 * "where do I save a strategy?" is answered by the group, before any link is read.
 */
function Group(props: { title: string; children: React.ReactNode }): React.JSX.Element {
  return (
    <div className="flex flex-col gap-0.5">
      <h2 className="px-3 pt-4 pb-1 text-[0.65rem] font-semibold tracking-widest text-slate-600 uppercase">
        {props.title}
      </h2>
      {props.children}
    </div>
  )
}

export function App(): React.JSX.Element {
  // The basket launched most recently, if any. There is no `GET /baskets`, so without this link a
  // basket would be reachable only by pasting its URL back — and the reader who just launched one
  // is exactly the reader who wants to return to it.
  const basketId = useSession((state) => state.basketId)
  const basketLabel = useSession((state) => state.basketLabel)
  // The same thread for a study, and for the same reason: there is no `GET /studies` either.
  const studyId = useSession((state) => state.studyId)
  const studyLabel = useSession((state) => state.studyLabel)
  // And again for a walk-forward, which needs the thread most of the three: it runs for minutes,
  // so the reader launches one and goes to look at something else.
  const walkForwardId = useSession((state) => state.walkForwardId)
  const walkForwardLabel = useSession((state) => state.walkForwardLabel)
  const threads = basketId !== null || studyId !== null || walkForwardId !== null

  return (
    <div className="flex min-h-screen bg-slate-950 text-slate-100">
      {/* A fixed column rather than a collapsing one: this is a desk tool on a wide screen, and a
          sidebar that hides itself is a sidebar whose state has to be remembered and got wrong. */}
      <aside className="w-56 shrink-0 border-r border-slate-800 px-2 pb-8">
        <h1 className="px-3 py-4 text-lg font-bold tracking-tight">TradeForge</h1>
        <nav className="flex flex-col">
          {/* The shelf, and the two ways onto it: build something new, or re-run what is already
              saved over a different instrument or window — which costs no new version. */}
          <Group title="Strategies">
            <NavLink to="/catalog" className={navClass}>
              Catalogue
            </NavLink>
            <NavLink to="/" end className={navClass}>
              New backtest
            </NavLink>
            <NavLink to="/launch" className={navClass}>
              Re-run saved
            </NavLink>
          </Group>
          {/* One strategy over several markets — whether it travels, which no single run can
              answer. And the same question turned inward: vary the strategy's own parameters, and
              ask whether a result is a property of the method or of the corner that was picked. */}
          <Group title="Experiments">
            <NavLink to="/basket" className={navClass}>
              Basket
            </NavLink>
            <NavLink to="/study" className={navClass}>
              Study
            </NavLink>
            <NavLink to="/runs" className={navClass}>
              Run log
            </NavLink>
          </Group>
          {/* The only screen here about money that is already at risk. Everything above it is a
              question about the past; this one is a machine that is trading right now, and it
              carries the two handles for stopping it. Collect sits beside it because both are
              about the world outside this database rather than about a result inside it. */}
          <Group title="Markets">
            <NavLink to="/live" className={navClass}>
              Live
            </NavLink>
            <NavLink to="/collect" className={navClass}>
              Collect
            </NavLink>
          </Group>
          {threads && (
            <Group title="Recent">
              {basketId !== null && (
                <NavLink to={`/baskets/${basketId}`} className={navClass}>
                  ▸ {basketLabel}
                </NavLink>
              )}
              {studyId !== null && (
                <NavLink to={`/studies/${studyId}`} className={navClass}>
                  ▸ {studyLabel}
                </NavLink>
              )}
              {walkForwardId !== null && (
                <NavLink to={`/walkforwards/${walkForwardId}`} className={navClass}>
                  ▸ {walkForwardLabel}
                </NavLink>
              )}
            </Group>
          )}
        </nav>
      </aside>
      {/* `min-w-0` on the flex child, not decoration: without it a table that scrolls inside its
          own box instead widens this column, and the page scrolls sideways as a whole — which is
          the one thing the run log's `overflow-x-auto` exists to prevent. */}
      <main className="min-w-0 flex-1 px-8 py-8">
        <div className="mx-auto max-w-5xl">
          <Routes>
            <Route path="/" element={<StrategyBuilder />} />
            {/* The same screen, opened on a saved strategy. One component rather than a viewer and
                an editor, because a builder that cannot show what it produced is how the two drift. */}
            <Route path="/strategies/:id" element={<StrategyBuilder />} />
            <Route path="/catalog" element={<StrategyCatalog />} />
            <Route path="/launch" element={<LaunchBacktest />} />
            <Route path="/basket" element={<LaunchBasket />} />
            <Route path="/baskets/:id" element={<BasketResult />} />
            <Route path="/study" element={<LaunchStudy />} />
            <Route path="/studies/:id" element={<StudyResult />} />
            <Route path="/walkforwards/:id" element={<WalkForwardResult />} />
            <Route path="/collect" element={<CollectSymbol />} />
            <Route path="/runs" element={<RunLog />} />
            <Route path="/live" element={<LiveSessions />} />
            <Route path="/results/:id" element={<Results />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}
