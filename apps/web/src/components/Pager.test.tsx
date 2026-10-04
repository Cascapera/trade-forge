import { fireEvent, render, screen } from '@testing-library/react'

import { Pager } from './Pager'
import { clampOffset, pageOf } from './paging'

describe('Pager', () => {
  it('renders nothing when everything fits on one page', () => {
    const { container } = render(
      <Pager label="Rows" offset={0} limit={10} total={10} onOffset={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('says which rows show and steps by a page', () => {
    const onOffset = vi.fn()
    render(<Pager label="Rows" offset={10} limit={10} total={25} onOffset={onOffset} />)

    expect(screen.getByText('11–20 of 25')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next →' }))
    fireEvent.click(screen.getByRole('button', { name: '← Previous' }))
    expect(onOffset.mock.calls).toEqual([[20], [0]])
  })

  it('cannot step past either end', () => {
    render(<Pager label="Rows" offset={20} limit={10} total={25} onOffset={vi.fn()} />)
    expect(screen.getByText('21–25 of 25')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next →' })).toBeDisabled()
  })
})

describe('pageOf and clampOffset', () => {
  it('slices a page and steps back when the list shrank under it', () => {
    expect(pageOf([1, 2, 3, 4, 5], 2, 2)).toEqual([3, 4])
    expect(clampOffset(20, 10, 25)).toBe(20)
    expect(clampOffset(20, 10, 15)).toBe(10)
    expect(clampOffset(10, 10, 0)).toBe(0)
  })
})
