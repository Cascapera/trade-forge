import { NavLink, Navigate, Route, Routes } from 'react-router-dom'

import { BasketResult } from './screens/BasketResult'
import { CollectSymbol } from './screens/CollectSymbol'
import { LaunchBacktest } from './screens/LaunchBacktest'
import { LaunchBasket } from './screens/LaunchBasket'
import { Results } from './screens/Results'
import { LaunchStudy } from './screens/LaunchStudy'
import { RunLog } from './screens/RunLog'
import { StrategyBuilder } from './screens/StrategyBuilder'
import { StudyResult } from './screens/StudyResult'
import { WalkForwardResult } from './screens/WalkForwardResult'
import { useSession } from './store'

function navClass({ isActive }: { isActive: boolean }): string {
  return isActive ? 'font-semibold text-sky-400' : 'text-slate-400 hover:text-slate-200'
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

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800">
        <div className="mx-auto flex max-w-5xl items-center gap-6 px-6 py-4">
          <h1 className="text-lg font-bold tracking-tight">TradeForge</h1>
          <nav className="flex gap-4 text-sm">
            {/* The first route builds a strategy and runs it in one go; the second re-runs the one
                already saved this session over a different instrument or window, which costs no
                new version. The third runs it over several markets at once — the question of
                whether it travels, which no single run can answer. The fourth varies the
                strategy's own parameters instead — the same question turned inward, asking
                whether a result is a property of the method or of the corner that was picked.
                The fifth looks back at everything already run. */}
            <NavLink to="/" end className={navClass}>
              New backtest
            </NavLink>
            <NavLink to="/launch" className={navClass}>
              Re-run saved
            </NavLink>
            <NavLink to="/basket" className={navClass}>
              Basket
            </NavLink>
            <NavLink to="/study" className={navClass}>
              Study
            </NavLink>
            <NavLink to="/runs" className={navClass}>
              Run log
            </NavLink>
            {/* Last, because it is upstream of everything else and reached least often: a symbol
                is collected once and backtested for weeks. */}
            <NavLink to="/collect" className={navClass}>
              Collect
            </NavLink>
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
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-6 py-8">
        <Routes>
          <Route path="/" element={<StrategyBuilder />} />
          {/* The same screen, opened on a saved strategy. One component rather than a viewer and
              an editor, because a builder that cannot show what it produced is how the two drift. */}
          <Route path="/strategies/:id" element={<StrategyBuilder />} />
          <Route path="/launch" element={<LaunchBacktest />} />
          <Route path="/basket" element={<LaunchBasket />} />
          <Route path="/baskets/:id" element={<BasketResult />} />
          <Route path="/study" element={<LaunchStudy />} />
          <Route path="/studies/:id" element={<StudyResult />} />
          <Route path="/walkforwards/:id" element={<WalkForwardResult />} />
          <Route path="/collect" element={<CollectSymbol />} />
          <Route path="/runs" element={<RunLog />} />
          <Route path="/results/:id" element={<Results />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}
