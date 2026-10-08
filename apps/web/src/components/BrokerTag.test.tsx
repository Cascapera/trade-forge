import { render, screen } from '@testing-library/react'

import { BrokerTag } from './BrokerTag'

describe('BrokerTag', () => {
  it('names the broker whose terminal lists the symbol', () => {
    render(<BrokerTag broker="tradeview" />)
    expect(screen.getByText('tradeview')).toHaveAttribute('title', 'listed by tradeview')
  })

  it('draws nothing for a server nobody registered', () => {
    const { container } = render(<BrokerTag broker={null} />)
    expect(container).toBeEmptyDOMElement()
  })
})
