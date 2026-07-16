import { screen } from '@testing-library/react'

import { App } from './App'
import { renderWithProviders } from './test-utils'

describe('App', () => {
  it('renders the builder at the root', () => {
    renderWithProviders(<App />, '/')
    expect(screen.getByRole('heading', { name: 'TradeForge' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Build a strategy' })).toBeInTheDocument()
  })

  it('redirects an unknown route to the builder', () => {
    renderWithProviders(<App />, '/nowhere')
    expect(screen.getByRole('heading', { name: 'Build a strategy' })).toBeInTheDocument()
  })
})
