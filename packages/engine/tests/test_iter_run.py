"""`iter_run`: the loop as it runs live, and the proof that it is the same loop.

`run()` is the backtest, and it is now written as an accumulator over `iter_run()`. That makes
the interesting claims about the pair rather than about either one:

* whatever `run()` reports, a consumer draining `iter_run()` can rebuild exactly;
* the loop pulls one candle at a time and hands back a result before pulling the next, which
  is the entire reason a live session can exist at all;
* the engine's decimal context is installed for a bar's work and released before the hand-over.

The last one is the one no existing test could have caught. `ENGINE_CONTEXT` is a precision of
28 with `ROUND_HALF_EVEN`, and that is also CPython's *default* — so under a normal test run,
pinning the context and forgetting to pin it produce identical numbers. Every assertion here
about it therefore runs under a deliberately hostile ambient context.
"""

import datetime as dt
from collections.abc import Iterator
from dataclasses import asdict
from decimal import Decimal, getcontext, localcontext

import pytest

from tradeforge_engine.domain import Candle
from tradeforge_engine.loop import ENGINE_CONTEXT, BarOutcome, iter_run, run
from tradeforge_engine.testing import (
    EURUSD,
    HOUR,
    FixedRisk,
    ImmediateFillBroker,
    ScriptedStrategy,
    close_out,
    entry,
    rising,
)

SCRIPT = {5: [entry()], 12: [close_out()], 20: [entry()], 33: [close_out()]}


def a_broker() -> ImmediateFillBroker:
    return ImmediateFillBroker(costs=Decimal("3.50"))


def drive(candles: list[Candle], broker: ImmediateFillBroker) -> list[BarOutcome]:
    return list(
        iter_run(
            candles=candles,
            timeframe=HOUR,
            instrument=EURUSD,
            strategy=ScriptedStrategy(script=SCRIPT),
            broker=broker,
            risk=FixedRisk(volume=Decimal("0.5")),
        )
    )


def test_draining_iter_run_rebuilds_exactly_what_run_reports() -> None:
    """The equivalence that makes one loop enough for both modes.

    Not "similar": the fills, the curve and the trades are compared field by field, the way
    `test_determinism` compares runs. A live session that rebuilt a slightly different curve
    from the same bars would be reporting on a system nobody backtested.
    """
    reported = run(
        candles=rising(40),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(script=SCRIPT),
        broker=a_broker(),
        risk=FixedRisk(volume=Decimal("0.5")),
    )

    broker = a_broker()
    outcomes = drive(rising(40), broker)

    assert [asdict(f) for o in outcomes for f in o.fills] == [asdict(f) for f in reported.fills]
    assert [asdict(o.equity) for o in outcomes] == [asdict(p) for p in reported.equity_curve]
    assert len(outcomes) == reported.candles_processed
    assert [asdict(t) for t in broker.trades()] == [asdict(t) for t in reported.trades]


def test_the_index_counts_the_bars_in_order() -> None:
    outcomes = drive(rising(6), a_broker())
    assert [o.index for o in outcomes] == [0, 1, 2, 3, 4, 5]
    assert [o.candle.time for o in outcomes] == [c.time for c in rising(6)]


def test_a_bar_is_handed_back_before_the_next_one_is_pulled() -> None:
    """The live property, stated as laziness — and it has nothing to do with speed.

    A live session's candle source blocks: the generator behind it sits on a Redis stream and
    yields when a bar closes, which may be an hour away. A loop that pulled its input eagerly
    would sit inside that source for ever and the session would produce nothing — not slowly,
    but never. So the claim is not "it is fast": after N results have been taken, the source
    has been asked for exactly N bars.

    The source is checked in the middle, at N=2, and that is deliberate. Drained to
    exhaustion an eager loop and a lazy one both report 6; the scenario that separates them
    has to stop before the end.
    """
    pulled: list[Candle] = []

    def watched() -> Iterator[Candle]:
        for candle in rising(6):
            pulled.append(candle)
            yield candle

    outcomes = iter_run(
        candles=watched(),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(script=SCRIPT),
        broker=a_broker(),
        risk=FixedRisk(volume=Decimal("0.5")),
    )

    assert pulled == [], "the loop touched the source before anyone asked it for a bar"

    first = next(outcomes)
    assert len(pulled) == 1
    assert first.candle == pulled[0]

    next(outcomes)
    assert len(pulled) == 2


def test_a_bad_timeframe_is_refused_at_the_call_not_at_the_first_bar() -> None:
    """A generator's body does not run until it is advanced, so a `raise` written inside one
    arrives whenever the consumer gets round to iterating. For a live session that is the
    difference between a configuration error at start-up and one an hour later, mid-stream."""
    with pytest.raises(ValueError, match="timeframe must be positive"):
        iter_run(
            candles=rising(4),
            timeframe=dt.timedelta(0),
            instrument=EURUSD,
            strategy=ScriptedStrategy(script={}),
            broker=a_broker(),
            risk=FixedRisk(volume=Decimal("0.5")),
        )


def as_text(outcomes: list[BarOutcome]) -> list[str]:
    """The equity curve as the text it would be written down as.

    ⚠️ **Text, not `Decimal`.** `Decimal("10046.5") == Decimal("10046.50")` is `True` — equality
    compares numerically, so a lost quantum is invisible to it. That is not a hypothetical
    here: measured, the scenario below prints `10046.50` under the engine's pinned context and
    `10046.5` under a `prec=6` one, and those are the *same number*. An assertion comparing
    `Decimal`s passes against an engine that stopped pinning anything at all, which is exactly
    what the first version of this test did.
    """
    return [str(outcome.equity.equity) for outcome in outcomes]


def test_the_engine_pins_its_arithmetic_on_every_bar() -> None:
    """The live path's version of `test_an_ambient_decimal_context_cannot_change_the_result`.

    `run()` used to pin the context once, around the whole drain. A generator cannot: the
    caller's `with` block has long exited by the time the body runs, so the loop has to pin it
    per bar. If that pin were lost this is where it would show — and only here, because the
    assertion runs while `prec=6` is still installed.
    """
    baseline = as_text(drive(rising(40), a_broker()))
    assert any(len(value.split(".")[-1]) == 2 for value in baseline), (
        "no value in the scenario carries a trailing quantum, so prec=6 cannot change its text"
    )

    with localcontext() as hostile:
        hostile.prec = 6  # brutally low; enough to drop the last place of a 7-digit equity
        under_pressure = as_text(drive(rising(40), a_broker()))
        assert getcontext().prec == 6, "the engine leaked its own context back to the caller"

    assert under_pressure == baseline


def test_the_context_is_released_before_the_bar_is_handed_over() -> None:
    """The other half: a consumer must not find itself running under the engine's precision.

    A live consumer writes to a database between bars. Holding `ENGINE_CONTEXT` across the
    `yield` would silently reconfigure that code — arithmetic outside the engine, rounded by
    the engine's rules, with nothing anywhere saying so.
    """
    handed_over = 0

    with localcontext() as ambient:
        ambient.prec = 6
        for _outcome in iter_run(
            candles=rising(4),
            timeframe=HOUR,
            instrument=EURUSD,
            strategy=ScriptedStrategy(script=SCRIPT),
            broker=a_broker(),
            risk=FixedRisk(volume=Decimal("0.5")),
        ):
            handed_over += 1
            assert getcontext().prec == 6, "the engine held its context across the hand-over"

    # ⚠️ Without this the test passes on a loop that yields nothing at all — the assertion
    # inside a body that never runs proves only that it never ran.
    assert handed_over == 4
    assert ENGINE_CONTEXT.prec != 6, "the hostile context stopped being hostile"


def test_a_bar_reports_its_own_fills_not_the_runs() -> None:
    """`BarOutcome.fills` is the tuple the strategy was shown, so a consumer persisting them
    bar by bar writes each fill exactly once. Accumulating the run's would rewrite history."""
    outcomes = drive(rising(40), a_broker())
    seen = [fill for outcome in outcomes for fill in outcome.fills]

    assert seen, "the scenario produced no fills, so it proves nothing about them"
    assert len(seen) == len({(f.order.reason, f.time) for f in seen}), "a fill was reported twice"
    for outcome in outcomes:
        for fill in outcome.fills:
            assert fill.time == outcome.candle.time
