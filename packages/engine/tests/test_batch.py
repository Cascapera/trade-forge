"""`run_batch` (ADR-0029): many runs over one market, the reading advanced once per bar.

The one claim a batch rests on: every member's `RunResult` is the one `run` gives the same
document on its own — fills, trades, equity, the final account. Held over seeded walks that trade
(which refuse nothing: the refusals travel through the same `iter_run` and are held there, by
`test_loop`), with the documents a sweep would vary; and the ways a batch can go wrong held
alongside: one member failing, the reading failing, a document that cannot share a reading.
"""

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.batch import BatchMember, run_batch
from tradeforge_engine.domain import Candle
from tradeforge_engine.errors import EngineError
from tradeforge_engine.loop import run
from tradeforge_engine.reading import MarketReading
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.setup_factory import build_setup, reading_for, shared_reading
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


def _grid(*, htf: bool, offset: int = 0) -> list[dict[str, Any]]:
    """What a sweep varies over one market: entry, side, breakeven, the ladder — not the reading."""
    filtered: dict[str, object] = {"htf": "H4", "htf_offset": offset} if htf else {}
    return [
        _document(
            kind,
            {
                "entry_point": entry,
                "side": side,
                "breakeven_at_r": breakeven,
                **({"max_bos": 1} if kind == "structure_continuation" else {}),
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


def _alone(document: dict[str, Any], candles: list[Candle], *, snapshots: bool) -> Any:
    return run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(document),
        broker=_broker(),
        risk=PercentRiskManager(percent=Decimal(1)),
        record_snapshots=snapshots,
    )


def _batched(
    documents: list[dict[str, Any]], candles: list[Candle], *, snapshots: bool
) -> list[Any]:
    keys = {shared_reading(document["setup"], timeframe=HOUR) for document in documents}
    (key,) = keys
    assert key is not None
    reading = reading_for(key, timeframe=HOUR)
    members = [
        BatchMember(
            strategy=compile_strategy(document, reading=reading),
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
    )


# (seed, filtered, broker clock): the last one cuts the bars above three hours off UTC.
_WALKS = [(0, True, 0), (4, True, 0), (10, True, 0), (4, True, 3), (0, False, 0), (1, False, 0)]


@pytest.mark.parametrize("snapshots", [False, True], ids=["sweep", "snapshots"])
@pytest.mark.parametrize(("seed", "htf", "offset"), _WALKS)
def test_every_member_comes_out_as_it_does_alone(
    seed: int, htf: bool, offset: int, snapshots: bool
) -> None:
    candles = _walk(seed)
    documents = _grid(htf=htf, offset=offset)

    outcomes = _batched(documents, candles, snapshots=snapshots)

    assert [outcome.error for outcome in outcomes] == [None] * len(documents)
    alone = [_alone(document, candles, snapshots=snapshots) for document in documents]
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
        def key(params: dict[str, object]) -> object:
            return shared_reading({"type": "structure_choch", "params": params}, timeframe=HOUR)

        assert key({}) == (None, dt.timedelta(0))
        assert key({"htf": "H4"}) == (4 * HOUR, dt.timedelta(0))
        assert key({"htf": "D1"}) != key({"htf": "H4"})
        assert key({"htf": "H4", "htf_offset": 3}) != key({"htf": "H4"})

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
