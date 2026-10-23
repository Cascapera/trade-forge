"""Many runs over one market, a bar at a time, reading the market once (ADR-0029).

A sweep runs the same chart tens of thousands of times, and every run used to work out the same
structure, the same regions and the same regions above on every bar. `run_batch` works them out
once: a leader advances one `MarketReading` per bar, and each run then takes its bar through the
very `iter_run` a run on its own goes through — the same five steps, the same `on_bar` — reading
that reading instead of building its own. What each run keeps of its bars is `RunRecorder`'s, the
one `run` uses, so a run's result is the same whichever way it was run.

⚠️ **The leader goes first on every bar.** A setup handed a shared reading refuses one that is not
at the bar it is reading (`MarketReading.read_at`), behind *or* ahead, so a leader out of step
fails loudly instead of handing every run another bar's market.

⚠️ **One run's failure is that run's alone.** A run that raises is dropped from the batch with
its error, and the others go on; a failure of the reading itself fails every run, since every
run reads it.
"""

import datetime as dt
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import localcontext

from tradeforge_engine.domain import Candle, InstrumentSpec
from tradeforge_engine.loop import (
    ENGINE_CONTEXT,
    BarOutcome,
    QuietBars,
    RunRecorder,
    RunResult,
    iter_run,
)
from tradeforge_engine.protocols import Broker, RiskManager, Strategy
from tradeforge_engine.reading import MarketReading


@dataclass(frozen=True, slots=True)
class BatchMember:
    """One run of a batch: its strategy — built on the batch's reading — its broker and its risk."""

    strategy: Strategy
    broker: Broker
    risk: RiskManager


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """What one member came to: its result, or the error that ended it. Never both."""

    result: RunResult | None
    error: Exception | None


class _Feed:
    """The one bar `iter_run` pulls next — handed in by the batch, a bar at a time."""

    def __init__(self) -> None:
        self.bar: Candle | None = None

    def __iter__(self) -> "_Feed":
        return self

    def __next__(self) -> Candle:
        if self.bar is None:
            raise StopIteration
        bar, self.bar = self.bar, None
        return bar


class _Running:
    def __init__(self, member: BatchMember, bars: Iterator[BarOutcome], feed: _Feed) -> None:
        self.member = member
        self.bars = bars
        self.feed = feed
        # The broker says where its account opens (ADR-0030), as it does for `run`.
        self.recorder = RunRecorder(book_from=getattr(member.broker, "book_from", None))
        self.error: Exception | None = None


def run_batch(  # noqa: PLR0913 — keyword-only; each one names a real axis of a backtest
    *,
    candles: Sequence[Candle],
    timeframe: dt.timedelta,
    instrument: InstrumentSpec,
    reading: MarketReading,
    members: Sequence[BatchMember],
    record_snapshots: bool = True,
    quiet: QuietBars = QuietBars.SKIP,
) -> list[BatchOutcome]:
    """Run every member over `candles`, the reading advanced once per bar ahead of all of them.

    ⚠️ **Quiet bars are skipped by default** (`QuietBars.SKIP`): a member whose setup says nothing
    can happen on a bar, over an account that can fill nothing, takes the bar through
    `loop._quiet_step`. `SHADOW` runs every bar in full and refuses one called quiet that was not.

    Returns one outcome per member, in the members' order. Each member's strategy has to have
    been built on `reading` — one built on its own would read its own market and ignore this one,
    which the batch cannot see and so does not allow for.
    """
    running = []
    for member in members:
        feed = _Feed()
        bars = iter_run(
            candles=feed,
            timeframe=timeframe,
            instrument=instrument,
            strategy=member.strategy,
            broker=member.broker,
            risk=member.risk,
            record_snapshots=record_snapshots,
            quiet=quiet,
        )
        running.append(_Running(member, bars, feed))

    for candle in candles:
        try:
            with localcontext(ENGINE_CONTEXT):
                reading.advance(candle)
        except Exception as exc:  # noqa: BLE001 — every run reads it, so every run ends with it
            for run in running:
                if run.error is None:
                    run.error = exc
            break
        for run in running:
            if run.error is not None:
                continue
            run.feed.bar = candle
            try:
                run.recorder.add(next(run.bars))
            except Exception as exc:  # noqa: BLE001 — this run's failure, not the batch's
                run.error = exc

    # ⚠️ **Each run is taken past its last bar, as `run` takes it.** `run`'s `for` asks the
    # generator once more after the last bar and learns there is none; that resumption runs the
    # tail of the bar — the broker's refusals drained — before `StopIteration`. With the brokers
    # in this repo that tail reaches no `RunResult` (the drained refusals are the generator's own,
    # and `trades()`/`account()` never read them), so a mutant deleting this block survives. It
    # stays as a boundary, like the `localcontext` in `RunRecorder.result`: a broker whose
    # `refusals()` changed what it reports would need it, and a batch must leave every broker in
    # the state `run` leaves it in.
    for run in running:
        if run.error is None:
            try:
                next(run.bars)
            except StopIteration:
                pass
            except Exception as exc:  # noqa: BLE001 — this run's failure, not the batch's
                run.error = exc

    return [
        BatchOutcome(result=None, error=run.error)
        if run.error is not None
        else BatchOutcome(result=run.recorder.result(run.member.broker), error=None)
        for run in running
    ]


__all__ = ["BatchMember", "BatchOutcome", "run_batch"]
