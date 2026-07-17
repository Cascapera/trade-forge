"""Determinism: the same input produces the same output. Byte for byte.

This is the acceptance criterion of PR-103, and it is not a nicety. A backtest you cannot
reproduce is not a measurement — it is an anecdote. Every decision in `packages/engine`
serves this one property:

* no clock (time arrives in the candles),
* no randomness,
* no I/O of any kind — the package has zero dependencies,
* frozen domain objects, so nothing can be mutated behind the loop's back,
* `Decimal`, not `float`, and under the engine's own pinned precision,
* no set or dict iteration feeding a result.

**Two runs in one process is not enough to prove that last point**, and it is worth being
precise about why: `PYTHONHASHSEED` is fixed for the lifetime of a process, so a `set` of
strings iterates in the same order in both runs. The bytes match because both runs were
corrupted identically. So the real test spawns a **second interpreter with a different hash
seed** and compares the bytes across the process boundary.
"""

import json
import os
import subprocess
import sys
from dataclasses import asdict
from decimal import Decimal, getcontext, localcontext
from typing import Any

from tradeforge_engine.loop import RunResult, run
from tradeforge_engine.testing import (
    AAPL,
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


def canonical(result: RunResult) -> bytes:
    """A run, as bytes. `default=str` renders every Decimal exactly, with no float step."""
    return json.dumps(asdict(result), sort_keys=True, default=str).encode()


def a_run() -> RunResult:
    return run(
        candles=rising(40),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(script=SCRIPT),
        broker=ImmediateFillBroker(costs=Decimal("3.50")),
        risk=FixedRisk(volume=Decimal("0.5")),
    )


# Run in a fresh interpreter, so its hash seed differs from this one's.
SUBPROCESS_RUN = """
import hashlib, json, sys
from dataclasses import asdict
from decimal import Decimal
from tradeforge_engine.loop import run
from tradeforge_engine.testing import (
    EURUSD, HOUR, FixedRisk, ImmediateFillBroker, ScriptedStrategy, close_out, entry, rising,
)

result = run(
    candles=rising(40),
    timeframe=HOUR,
    instrument=EURUSD,
    strategy=ScriptedStrategy(
        script={5: [entry()], 12: [close_out()], 20: [entry()], 33: [close_out()]}
    ),
    broker=ImmediateFillBroker(costs=Decimal("3.50")),
    risk=FixedRisk(volume=Decimal("0.5")),
)
sys.stdout.write(json.dumps(asdict(result), sort_keys=True, default=str))
"""


def test_two_runs_of_the_same_input_are_identical_byte_for_byte() -> None:
    assert canonical(a_run()) == canonical(a_run())


def test_the_result_is_identical_in_a_second_interpreter_with_a_different_hash_seed() -> None:
    """The test that actually earns the word "deterministic".

    Same process, `PYTHONHASHSEED` is fixed — so a set iterating in a wrong-but-stable order
    yields identical bytes twice and the test smiles. Across processes with different seeds,
    it cannot.
    """
    environment = {**os.environ, "PYTHONHASHSEED": "1"}
    first = subprocess.run(  # noqa: S603
        [sys.executable, "-c", SUBPROCESS_RUN],
        capture_output=True,
        check=True,
        env=environment,
    ).stdout

    environment["PYTHONHASHSEED"] = "424242"
    second = subprocess.run(  # noqa: S603
        [sys.executable, "-c", SUBPROCESS_RUN],
        capture_output=True,
        check=True,
        env=environment,
    ).stdout

    assert first == second
    assert first == canonical(a_run())


def test_the_result_is_identical_across_many_runs() -> None:
    """Once is luck."""
    assert len({canonical(a_run()) for _ in range(10)}) == 1


def test_changing_one_candle_changes_the_result() -> None:
    """The other half of determinism, and the one people forget.

    A function that returns the same bytes no matter what you feed it is *also* perfectly
    deterministic. This proves the run is actually a function of its input.
    """
    different = run(
        candles=rising(40, step="0.00200"),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(script=SCRIPT),
        broker=ImmediateFillBroker(costs=Decimal("3.50")),
        risk=FixedRisk(volume=Decimal("0.5")),
    )

    assert canonical(a_run()) != canonical(different)


def test_an_ambient_decimal_context_cannot_change_the_result() -> None:
    """`decimal.getcontext()` is global and mutable.

    Any library in the worker process that sets `getcontext().prec` would silently change
    every number the engine produces — without touching a line of it. The engine pins its own
    precision inside `run()`, so it does not matter what the process around it has done.
    """
    baseline = canonical(a_run())

    with localcontext() as context:
        context.prec = 6  # brutally low; enough to ruin a five-decimal price
        under_a_hostile_context = canonical(a_run())

        # Inside the block, and that matters: the assertion has to run where the hostile
        # context is still installed. Outside it, `prec != 6` is trivially true and the test
        # proves nothing — which is exactly what the first version of it did.
        assert getcontext().prec == 6, "the engine leaked its own context back to the caller"

    assert under_a_hostile_context == baseline


def test_the_engine_never_reads_a_clock() -> None:
    """Time comes from the candles. A run today and the same run next year agree."""
    first = a_run()

    assert [point.time for point in first.equity_curve] == [
        point.time for point in a_run().equity_curve
    ]
    assert first.equity_curve[0].time.year == 2024


def test_money_survives_serialisation_as_an_exact_decimal() -> None:
    """`Decimal("0.1") + Decimal("0.2")` is `Decimal("0.3")`. The float version is not."""
    payload: dict[str, Any] = json.loads(canonical(a_run()))
    equity = payload["final_account"]["equity"]

    assert "e" not in equity.lower()  # never scientific notation
    assert Decimal(equity) == a_run().final_account.equity


def test_a_stock_and_a_pair_are_both_deterministic() -> None:
    """Different tick arithmetic, same guarantee."""

    def stock_run() -> RunResult:
        return run(
            candles=rising(20, start="190.00", step="0.50"),
            timeframe=HOUR,
            instrument=AAPL,
            strategy=ScriptedStrategy(script={3: [entry(price="190.50")], 10: [close_out()]}),
            broker=ImmediateFillBroker(initial_capital=Decimal(50_000), instrument=AAPL),
            risk=FixedRisk(volume=Decimal(100)),
        )

    assert canonical(stock_run()) == canonical(stock_run())
