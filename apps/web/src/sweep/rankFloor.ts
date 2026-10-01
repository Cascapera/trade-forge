/**
 * The fewest trades a run needs, on each chart, to be ranked or chosen — his bar of 01/10.
 *
 * ⚠️ **A copy, by hand, of `RANK_MIN_TRADES` in `apps/api/src/tradeforge_api/ranking_floor.py`.**
 * The API serves no endpoint for it, and the screen needs it only to say what a blank floor field
 * means. The server applies its own; if the two drift, the placeholder lies and the run does not.
 * Change both together — `rankFloor.test.ts` pins the values the API's test pins.
 *
 * Not the floor for what a run keeps (`retention.MIN_TRADES`), which is zero on H4 and above.
 */
export const RANK_MIN_TRADES: Readonly<Record<string, number>> = {
  M1: 60,
  M5: 60,
  M15: 30,
  M30: 30,
  H1: 30,
  H4: 20,
  D1: 10,
  W1: 5,
}

/** What a blank floor field on `chart` stands for, as its placeholder says it. */
export function floorPlaceholder(chart: string): string {
  const floor = RANK_MIN_TRADES[chart]
  return floor === undefined ? 'default' : String(floor)
}
