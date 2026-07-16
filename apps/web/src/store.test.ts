import { useSession } from './store'

describe('session store', () => {
  it('sets and clears the current strategy', () => {
    useSession.getState().setStrategy('abc', 'MA cross')
    expect(useSession.getState().strategyId).toBe('abc')
    expect(useSession.getState().strategyName).toBe('MA cross')

    useSession.getState().clear()
    expect(useSession.getState().strategyId).toBeNull()
    expect(useSession.getState().strategyName).toBeNull()
  })
})
