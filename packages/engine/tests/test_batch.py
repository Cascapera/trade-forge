"""`run_batch` (ADR-0029): many runs over one market, the reading advanced once per bar.

The one claim a batch rests on: every member's `RunResult` is the one `run` gives the same
document on its own — fills, trades, equity, the final account. Held over seeded walks that trade
(which refuse nothing: the refusals travel through the same `iter_run` and are held there, by
`test_loop`), with the documents a sweep would vary; and the ways a batch can go wrong held
alongside: one member failing, the reading failing, a document that cannot share a reading.
"""

import datetime as dt
from decimal import Decimal, localcontext
from typing import Any

import pytest

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.batch import BatchMember, run_batch
from tradeforge_engine.domain import Candle, Side
from tradeforge_engine.errors import EngineError
from tradeforge_engine.loop import ENGINE_CONTEXT, QuietBars, iter_run, run
from tradeforge_engine.reading import MarketReading
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.setup_factory import build_setup, reading_for, shared_reading
from tradeforge_engine.setups import ChochQualifier, ContinuationQualifier
from tradeforge_engine.strategy import compile_strategy
from tradeforge_engine.testing import EURUSD, HOUR

from .test_market_reading import _walk

_KINDS = ("structure_choch", "structure_continuation")


def _document(kind: str, params: dict[str, object], *, rr: float | None = 2) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "batched",
        "timeframe": "H1",
        "setup": {"type": kind, "params": params},
        **(
            {"exit": {"take_profit": {"type": "risk_multiple", "params": {"rr": rr}}}} if rr else {}
        ),
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def _grid(*, htf: bool) -> list[dict[str, Any]]:
    """What a sweep varies over one market: entry, side, breakeven, the ladder — not the reading."""
    filtered: dict[str, object] = {"htf": "H4"} if htf else {}
    return [
        _document(
            kind,
            {
                "entry_point": entry,
                "side": side,
                "breakeven_at_r": breakeven,
                **({"max_bos": 1} if kind == "structure_continuation" else {}),
                # The age rule off (29/09): these walks are short, and what is tested here is that
                # a batch, a shared reading or a warm-up changes nothing — which needs trades.
                "min_bars_to_touch": 1,
                **filtered,
            },
        )
        for kind in _KINDS
        for entry in ("edge", "midpoint", "return_pass")
        for side in ("both", "long")
        for breakeven in (None, 1.0)
    ]


def _broker() -> BacktestBroker:
    return BacktestBroker(
        instrument=EURUSD, initial_capital=Decimal(10_000), take_profit_rr=Decimal(2)
    )


def _alone(
    document: dict[str, Any],
    candles: list[Candle],
    *,
    snapshots: bool,
    clock: dt.timedelta = dt.timedelta(0),
) -> Any:
    return run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(document, server_offset=clock),
        broker=_broker(),
        risk=PercentRiskManager(percent=Decimal(1)),
        record_snapshots=snapshots,
    )


def _batched(
    documents: list[dict[str, Any]],
    candles: list[Candle],
    *,
    snapshots: bool,
    clock: dt.timedelta = dt.timedelta(0),
) -> list[Any]:
    keys = {
        shared_reading(document["setup"], timeframe=HOUR, server_offset=clock)
        for document in documents
    }
    (key,) = keys
    assert key is not None
    reading = reading_for(key, timeframe=HOUR)
    members = [
        BatchMember(
            strategy=compile_strategy(document, server_offset=clock, reading=reading),
            broker=_broker(),
            risk=PercentRiskManager(percent=Decimal(1)),
        )
        for document in documents
    ]
    return run_batch(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        reading=reading,
        members=members,
        record_snapshots=snapshots,
        # Named, not left to the default: this equality is the proof the skip changes nothing.
        quiet=QuietBars.SKIP,
    )


# (seed, filtered, broker clock): the last one cuts the bars above three hours off UTC.
# The filtered walks are the seeds that still trade since 29/09: a break now takes the resting order
# with it, and a withdrawn order spends the region above's one chance — seeds 0 and 10 stopped
# trading under a filter altogether, and a batch equal to its runs over no trades proves nothing.
_WALKS = [(4, True, 0), (5, True, 0), (9, True, 0), (4, True, 3), (0, False, 0), (1, False, 0)]


@pytest.mark.parametrize("snapshots", [False, True], ids=["sweep", "snapshots"])
@pytest.mark.parametrize(("seed", "htf", "offset"), _WALKS)
def test_every_member_comes_out_as_it_does_alone(
    seed: int, htf: bool, offset: int, snapshots: bool
) -> None:
    candles = _walk(seed)
    documents = _grid(htf=htf)
    # The broker's clock is the instrument's (30/09), handed to every run rather than written in
    # its document.
    clock = dt.timedelta(hours=offset)

    outcomes = _batched(documents, candles, snapshots=snapshots, clock=clock)

    assert [outcome.error for outcome in outcomes] == [None] * len(documents)
    alone = [_alone(document, candles, snapshots=snapshots, clock=clock) for document in documents]
    assert [outcome.result for outcome in outcomes] == alone
    # The walk has to have traded, or the equality is between empty results.
    assert sum(len(result.trades) for result in alone) > 0


class _FailsOnBar:
    """A member whose strategy raises on the bar it is told to — any run's own failure."""

    def __init__(self, inner: Any, at: int) -> None:
        self._inner = inner
        self._at = at
        self._seen = 0

    def on_bar(self, context: Any) -> Any:
        self._seen += 1
        if self._seen == self._at:
            raise RuntimeError("this member broke")
        return self._inner.on_bar(context)


def test_a_member_that_fails_ends_alone_and_the_others_go_on() -> None:
    candles = _walk(0)
    documents = _grid(htf=True)[:4]
    reading = reading_for(
        shared_reading(documents[0]["setup"], timeframe=HOUR) or (None, dt.timedelta(0)),
        timeframe=HOUR,
    )
    strategies = [compile_strategy(document, reading=reading) for document in documents]
    strategies[1] = _FailsOnBar(strategies[1], at=700)
    outcomes = run_batch(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        reading=reading,
        members=[
            BatchMember(
                strategy=strategy, broker=_broker(), risk=PercentRiskManager(percent=Decimal(1))
            )
            for strategy in strategies
        ],
        record_snapshots=False,
    )

    assert outcomes[1].result is None
    assert isinstance(outcomes[1].error, RuntimeError)
    for index in (0, 2, 3):
        assert outcomes[index].error is None
        assert outcomes[index].result == _alone(documents[index], candles, snapshots=False)


def test_a_reading_that_fails_ends_every_member() -> None:
    """Every member reads it, so none can go on without it."""
    candles = _walk(0, count=50)
    documents = _grid(htf=False)[:3]

    class _Broken(MarketReading):
        def advance(self, candle: Candle) -> None:
            if candle is candles[20]:
                raise RuntimeError("the market could not be read")
            super().advance(candle)

    reading = _Broken()
    outcomes = run_batch(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        reading=reading,
        members=[
            BatchMember(
                strategy=compile_strategy(document, reading=reading),
                broker=_broker(),
                risk=PercentRiskManager(percent=Decimal(1)),
            )
            for document in documents
        ],
    )

    assert all(outcome.result is None for outcome in outcomes)
    assert all(isinstance(outcome.error, RuntimeError) for outcome in outcomes)


def test_every_member_reads_the_batch_s_reading_and_not_one_of_its_own() -> None:
    """⚠️ The speed of a batch, not its results: a member that built a reading of its own would
    trade exactly the same and advance the market once per member — the whole gain gone without a
    word. A batch reading that falls silently behind is noticed only by members reading it."""
    candles = _walk(0, count=40)
    documents = _grid(htf=True)[:3]

    class _Stalls(MarketReading):
        def advance(self, candle: Candle) -> None:
            if candle is not candles[10]:
                super().advance(candle)

    reading = _Stalls(timeframe=HOUR, htf=4 * HOUR, htf_offset=dt.timedelta(0))
    outcomes = run_batch(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        reading=reading,
        members=[
            BatchMember(
                strategy=compile_strategy(document, reading=reading),
                broker=_broker(),
                risk=PercentRiskManager(percent=Decimal(1)),
            )
            for document in documents
        ],
    )

    assert all(isinstance(outcome.error, EngineError) for outcome in outcomes)


class TestSharedReading:
    def test_what_a_sweep_varies_reads_the_same_market(self) -> None:
        keys = {shared_reading(document["setup"], timeframe=HOUR) for document in _grid(htf=True)}
        assert keys == {(4 * HOUR, dt.timedelta(0))}

    def test_a_filter_of_another_timeframe_or_clock_reads_another(self) -> None:
        def key(params: dict[str, object], clock: int = 0) -> object:
            return shared_reading(
                {"type": "structure_choch", "params": params},
                timeframe=HOUR,
                server_offset=dt.timedelta(hours=clock),
            )

        assert key({}) == (None, dt.timedelta(0))
        assert key({"htf": "H4"}) == (4 * HOUR, dt.timedelta(0))
        assert key({"htf": "D1"}) != key({"htf": "H4"})
        # The clock is the instrument's (30/09), and it still tells two readings apart.
        assert key({"htf": "H4"}, clock=3) == (4 * HOUR, dt.timedelta(hours=3))
        assert key({"htf": "H4"}, clock=3) != key({"htf": "H4"})

    def test_a_document_that_states_the_clock_cannot_share_a_reading(self) -> None:
        # Refused by the factory (30/09), so read as "cannot share" — the same answer as any
        # block too malformed to parse, and the run then fails alone with the reason.
        stated = {"type": "structure_choch", "params": {"htf": "H4", "htf_offset": 3}}
        assert shared_reading(stated, timeframe=HOUR) is None

    def test_a_setup_without_a_reading_cannot_share_one(self) -> None:
        average = {"type": "mme9_breakout", "params": {"side": "long", "period": 9}}
        assert shared_reading(average, timeframe=HOUR) is None
        assert shared_reading({"type": "structure_choch", "params": "bad"}, timeframe=HOUR) is None
        with pytest.raises(EngineError, match="does not read a shared market"):
            build_setup(average, timeframe=HOUR, reading=MarketReading())

    def test_a_document_of_conditions_refuses_a_reading(self) -> None:
        document = {
            "schema_version": "1.0",
            "name": "conditions",
            "timeframe": "H1",
            "entry": {"long": {"op": "gt", "left": {"price": "close"}, "right": {"value": 1}}},
        }
        with pytest.raises(EngineError, match="only a setup reads a shared market"):
            compile_strategy(document, reading=MarketReading())


class TestQuietBars:
    """ADR-0029's second half: a batch skips the bars its setups call quiet, and it shows."""

    def _batch(self, candles: list[Candle], quiet: QuietBars) -> tuple[list[Any], list[int]]:
        documents = _grid(htf=True)
        key = shared_reading(documents[0]["setup"], timeframe=HOUR)
        assert key is not None
        reading = reading_for(key, timeframe=HOUR)
        calls: list[int] = []
        strategies = []
        for index, document in enumerate(documents):
            strategy = compile_strategy(document, reading=reading)
            calls.append(0)
            inner = strategy.on_bar

            def counted(context: Any, _inner: Any = inner, _at: int = index) -> Any:
                calls[_at] += 1
                return _inner(context)

            strategy.on_bar = counted  # type: ignore[method-assign]
            strategies.append(strategy)
        outcomes = run_batch(
            candles=candles,
            timeframe=HOUR,
            instrument=EURUSD,
            reading=reading,
            members=[
                BatchMember(
                    strategy=strategy,
                    broker=_broker(),
                    risk=PercentRiskManager(percent=Decimal(1)),
                )
                for strategy in strategies
            ],
            record_snapshots=False,
            quiet=quiet,
        )
        return [outcome.result for outcome in outcomes], calls

    def test_most_bars_are_skipped_and_nothing_changes(self) -> None:
        candles = _walk(4)
        skipped, calls = self._batch(candles, QuietBars.SKIP)
        full, every = self._batch(candles, QuietBars.OFF)

        assert skipped == full
        assert every == [len(candles)] * len(every)
        # Measured on this walk: the setups decide on a small share of the bars.
        assert sum(calls) < sum(every) / 2
        assert sum(len(result.trades) for result in full) > 0

    def test_in_shadow_every_bar_runs_in_full_and_none_called_quiet_did_anything(self) -> None:
        """The claim checked, not assumed: on each bar a setup calls quiet the full `on_bar` runs
        anyway, and a fill, an order or a refusal on it would be refused by the loop."""
        for seed in (0, 4, 10):
            candles = _walk(seed)
            shadowed, calls = self._batch(candles, QuietBars.SHADOW)
            full, _ = self._batch(candles, QuietBars.OFF)
            assert shadowed == full
            assert calls == [len(candles)] * len(calls)

    def test_a_setup_on_a_reading_of_its_own_is_never_quiet(self) -> None:
        """Its reading is read inside `on_bar`: before it, the break it holds is the last bar's."""
        candles = _walk(4)
        document = _grid(htf=True)[0]
        alone = run(
            candles=candles,
            timeframe=HOUR,
            instrument=EURUSD,
            strategy=compile_strategy(document),
            broker=_broker(),
            risk=PercentRiskManager(percent=Decimal(1)),
        )
        strategy = compile_strategy(document)
        assert strategy.quiet(candles[0]) is False  # type: ignore[attr-defined]
        broker = _broker()
        bars = list(
            iter_run(
                candles=candles,
                timeframe=HOUR,
                instrument=EURUSD,
                strategy=strategy,
                broker=broker,
                risk=PercentRiskManager(percent=Decimal(1)),
                quiet=QuietBars.SKIP,
            )
        )
        assert tuple(broker.trades()) == alone.trades
        assert tuple(bar.equity for bar in bars) == alone.equity_curve

    def test_an_account_with_an_order_or_a_position_is_not_idle(self) -> None:
        broker = _broker()
        assert broker.idle()
        broker._pending.append(object())  # type: ignore[arg-type]
        assert not broker.idle()

    def test_a_waiting_rung_is_quiet_only_while_the_gate_above_is_shut(self) -> None:
        """⚠️ The branch the walks never reach (the guardian's mutants M3, M8, M9): a rung still
        standing, nothing armed, no break. Only a shut gate makes that bar quiet — without a gate
        above, or with a side released, the full bar could arm the rung, and skipping it would
        lose the trade from the batch alone."""
        candles = _walk(0, count=3)
        reading = reading_for((4 * HOUR, dt.timedelta(0)), timeframe=HOUR)
        strategy: Any = compile_strategy(_grid(htf=True)[0], reading=reading)
        with localcontext(ENGINE_CONTEXT):
            reading.advance(candles[0])
        assert reading.break_ is None

        class _Rungs:
            def __init__(self, holds: bool) -> None:
                self.holds_rungs = holds

        strategy._qualifier = _Rungs(holds=False)
        assert strategy.quiet(candles[0]) is True

        strategy._qualifier = _Rungs(holds=True)
        assert strategy.quiet(candles[0]) is True  # the gate above is shut
        strategy._gate._releases[Side.LONG] = object()
        assert strategy.quiet(candles[0]) is False  # a side released: the rung could arm
        strategy._gate = None
        assert strategy.quiet(candles[0]) is False  # no gate above: nothing refuses the rung

        strategy._qualifier = object()  # a qualifier that cannot say
        assert strategy.quiet(candles[0]) is False

    @pytest.mark.parametrize("qualifier", [ChochQualifier, ContinuationQualifier])
    def test_a_qualifier_says_when_a_rung_is_waiting(self, qualifier: Any) -> None:
        """What `quiet` asks of the real qualifiers — a stub above would let them lie."""
        asked = qualifier()
        assert asked.holds_rungs is False
        asked._ladder = [object()]
        assert asked.holds_rungs is True
