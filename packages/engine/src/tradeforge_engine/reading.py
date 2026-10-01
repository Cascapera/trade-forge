"""The market as a structure setup reads it: its breaks, its regions, and the regions above.

⚠️ **Pure: a function of the bars, the setup's own timeframe, the timeframe above and the broker's
clock** — nothing a setup does reaches it (ADR-0029). That is what lets one reading serve many
runs of a sweep over the same market: a leader advances it once per bar, and every run reads it.
A run that is not part of a batch — a backtest on its own, paper, live — builds its own and
advances it itself, which is what every run did before 27/09, with the same objects in the same
order.
"""

import datetime as dt
from collections.abc import Sequence

from tradeforge_engine.domain import Candle
from tradeforge_engine.errors import EngineError
from tradeforge_engine.higher_timeframe import RegionTracker
from tradeforge_engine.structure import (
    MarketStructure,
    OrderBlock,
    OrderBlockDetector,
    StructureBreak,
    TrackedZone,
)


class MarketReading:
    """His structure and regions on the setup's own bars, and the tracker of the regions above.

    `advance` folds in one closed bar; `break_`, `marked`, `zones` and `regions` are what that
    bar left. A setup reads them and never writes: a reading shared by a batch hands every run
    the very same objects, and one run writing would change the market the others trade.
    """

    def __init__(
        self,
        *,
        timeframe: dt.timedelta | None = None,
        htf: dt.timedelta | None = None,
        htf_offset: dt.timedelta | None = None,
    ) -> None:
        if htf is not None and timeframe is None:
            raise ValueError("a higher-timeframe reading needs the setup's own timeframe")
        self._structure = MarketStructure()
        self._blocks = OrderBlockDetector()
        # ⚠️ No broker clock means UTC — the setup's default; a run hands over its instrument's.
        self._regions = (
            None
            if htf is None or timeframe is None
            else RegionTracker(
                base=timeframe,
                target=htf,
                offset=dt.timedelta(0) if htf_offset is None else htf_offset,
            )
        )
        self._break: StructureBreak | None = None
        self._marked: tuple[OrderBlock, ...] = ()
        self._at: dt.datetime | None = None

    @property
    def structure(self) -> MarketStructure:
        return self._structure

    @property
    def blocks(self) -> OrderBlockDetector:
        return self._blocks

    @property
    def regions(self) -> RegionTracker | None:
        """The tracker of the timeframe above, or `None` for a reading without one."""
        return self._regions

    @property
    def break_(self) -> StructureBreak | None:
        """The break the last bar confirmed, or `None`."""
        return self._break

    @property
    def marked(self) -> tuple[OrderBlock, ...]:
        """The regions the last bar's break revealed, primary first — `()` on most bars."""
        return self._marked

    @property
    def zones(self) -> Sequence[TrackedZone]:
        """Every region offered so far on the setup's own bars (`OrderBlockDetector.zones`)."""
        return self._blocks.zones

    def advance(self, candle: Candle) -> None:
        """Fold in one closed bar: the regions above, then this bar's structure and regions.

        The three readers are independent of one another — the tracker runs its own structure
        and detector on the bars above — so the order among them changes nothing; it is the
        order the setup always used.
        """
        if self._regions is not None:
            self._regions.observe(candle)
        self._break = self._structure.update(candle)
        self._marked = self._blocks.update(candle, self._break)
        self._at = candle.time

    def read_at(self, candle: Candle) -> None:
        """Refuse a reading that was not advanced to this very bar.

        ⚠️ A shared reading is advanced by its leader, not by the setup reading it; a leader one
        bar behind or ahead would hand every run of the batch another bar's market, and nothing
        downstream could tell. Asked by the setup on every bar it reads a shared reading.
        """
        if self._at != candle.time:
            raise EngineError(
                f"the market reading is at {self._at}, not at the bar being read ({candle.time})"
            )


__all__ = ["MarketReading"]
