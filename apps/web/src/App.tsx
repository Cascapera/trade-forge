import { NavLink, Navigate, Route, Routes } from 'react-router-dom'

import { BasketResult } from './screens/BasketResult'
import { LaunchBacktest } from './screens/LaunchBacktest'
import { LaunchBasket } from './screens/LaunchBasket'
import { Results } from './screens/Results'
import { RunLog } from './screens/RunLog'
import { StrategyBuilder } from './screens/StrategyBuilder'
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

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800">
        <div className="mx-auto flex max-w-5xl items-center gap-6 px-6 py-4">
          <h1 className="text-lg font-bold tracking-tight">TradeForge</h1>
          <nav className="flex gap-4 text-sm">
            {/* The first route builds a strategy and runs it in one go; the second re-runs the one
                already saved this session over a different instrument or window, which costs no
                new version. The third runs it over several markets at once — the question of
                whether it travels, which no single run can answer. The fourth looks back at
                everything already run. */}
            <NavLink to="/" end className={navClass}>
              New backtest
            </NavLink>
            <NavLink to="/launch" className={navClass}>
              Re-run saved
            </NavLink>
            <NavLink to="/basket" className={navClass}>
              Basket
            </NavLink>
            <NavLink to="/runs" className={navClass}>
              Run log
            </NavLink>
            {basketId !== null && (
              <NavLink to={`/baskets/${basketId}`} className={navClass}>
                ▸ {basketLabel}
              </NavLink>
            )}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-6 py-8">
        <Routes>
          <Route path="/" element={<StrategyBuilder />} />
          <Route path="/launch" element={<LaunchBacktest />} />
          <Route path="/basket" element={<LaunchBasket />} />
          <Route path="/baskets/:id" element={<BasketResult />} />
          <Route path="/runs" element={<RunLog />} />
          <Route path="/results/:id" element={<Results />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}
