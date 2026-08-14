import { screen } from '@testing-library/react'

import { App } from './App'
import { useSession } from './store'
import { renderWithProviders } from './test-utils'

afterEach(() => {
  useSession.getState().clear()
})

describe('App', () => {
  it('renders the builder at the root', () => {
    renderWithProviders(<App />, '/')
    expect(screen.getByRole('heading', { name: 'TradeForge' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Run a backtest' })).toBeInTheDocument()
  })

  it('redirects an unknown route to the builder', () => {
    renderWithProviders(<App />, '/nowhere')
    expect(screen.getByRole('heading', { name: 'Run a backtest' })).toBeInTheDocument()
  })

  it('routes to the basket launcher', () => {
    renderWithProviders(<App />, '/basket')
    // Its own heading, which is enough to prove the route resolves to this screen rather than
    // falling through to the catch-all redirect. It used to assert the "build a strategy first"
    // message, and that stopped being a fact about the route when the screen learned to offer
    // the strategies that already exist.
    expect(screen.getByRole('heading', { name: /Basket of/ })).toBeInTheDocument()
  })

  it('routes to the study launcher', () => {
    renderWithProviders(<App />, '/study')

    expect(screen.getByRole('heading', { name: 'Parameter study' })).toBeInTheDocument()
  })

  it('offers no link back to a basket until one has been launched', () => {
    renderWithProviders(<App />, '/')
    expect(screen.queryByRole('link', { name: /markets/ })).not.toBeInTheDocument()
  })

  it('leads back to the basket launched most recently', () => {
    // There is no `GET /baskets`, so this link is the only way back that is not a pasted URL.
    useSession.getState().setBasket('k1', 'Ponto Contínuo · 3 markets')
    renderWithProviders(<App />, '/')

    const link = screen.getByRole('link', { name: /Ponto Contínuo · 3 markets/ })
    expect(link).toHaveAttribute('href', '/baskets/k1')
  })
})
