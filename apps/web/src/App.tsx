import { NavLink, Navigate, Route, Routes } from 'react-router-dom'

import { LaunchBacktest } from './screens/LaunchBacktest'
import { Results } from './screens/Results'
import { StrategyBuilder } from './screens/StrategyBuilder'

function navClass({ isActive }: { isActive: boolean }): string {
  return isActive ? 'font-semibold text-sky-400' : 'text-slate-400 hover:text-slate-200'
}

export function App(): React.JSX.Element {
  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800">
        <div className="mx-auto flex max-w-5xl items-center gap-6 px-6 py-4">
          <h1 className="text-lg font-bold tracking-tight">TradeForge</h1>
          <nav className="flex gap-4 text-sm">
            <NavLink to="/" end className={navClass}>
              Strategy
            </NavLink>
            <NavLink to="/launch" className={navClass}>
              Backtest
            </NavLink>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-6 py-8">
        <Routes>
          <Route path="/" element={<StrategyBuilder />} />
          <Route path="/launch" element={<LaunchBacktest />} />
          <Route path="/results/:id" element={<Results />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}
