"""How many trades a run needs to be ranked or chosen — his bar for reading a sweep (2026-10-01).

Not the floor for what a run *keeps* (`retention.MIN_TRADES`), which is zero on H4 and above so
that any profit there keeps its trades. Keeping and ranking are two questions: a W1 run of two
trades is worth keeping to open later, and is not worth putting at the top of a ranking. The first
reserved-window tests ranked D1 and W1 runs of one to four trades, and the best "method" of a sweep
was a coin tossed twice.

Read by the reserved-window test and the sweep's walk-forward (`routers.sweeps.launch_holdout`) as
the default a request may override per chart, and by `GET /sweeps/{id}/runs`, which hides what is
under it unless asked for every run.
"""

from collections.abc import Mapping
from typing import Final

RANK_MIN_TRADES: Final[Mapping[str, int]] = {
    "M1": 60,
    "M5": 60,
    "M15": 30,
    "M30": 30,
    "H1": 30,
    "H4": 20,
    "D1": 10,
    "W1": 5,
}
"""The fewest trades a run needs, on each chart, to be ranked or chosen. Every chart the DSL names
has a line here, and a test fails the day one is added without one.

⚠️ **Mirrored by hand in the web** (`apps/web/src/sweep/rankFloor.ts`), for the placeholders of the
launchers' floor fields: change both, or the screen offers a default the API does not apply."""


__all__ = ["RANK_MIN_TRADES"]
