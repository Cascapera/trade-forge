"""Monte Carlo of a run's trades — the cases whose answer is known without simulating, and the
draw in blocks against the draw trade by trade (01/10)."""

import random
import time
from decimal import Decimal

import pytest

from tradeforge_api.montecarlo import (
    MIN_TRADES,
    block_fits,
    default_block_trades,
    simulate,
    simulate_in_blocks,
)


def rs(*values: str) -> list[Decimal]:
    return [Decimal(one) for one in values]


def test_too_few_trades_are_not_resampled() -> None:
    assert simulate(rs(*["1"] * (MIN_TRADES - 1)), paths=100, seed="s") is None


def test_every_trade_the_same_winner_has_one_answer_on_every_path() -> None:
    """Drawing from thirty +1 R trades can only ever deal thirty +1 R trades."""
    found = simulate(rs(*["1"] * 30), paths=200, seed="s")

    assert found is not None
    assert found.net_r.p5 == found.net_r.p99 == Decimal(30)
    assert found.drawdown_r.p99 == Decimal(0)
    assert found.losing_streak.p99 == Decimal(0)
    assert found.negative_share == Decimal(0)


def test_every_trade_the_same_loser_falls_all_the_way_on_every_path() -> None:
    found = simulate(rs(*["-1"] * 25), paths=200, seed="s")

    assert found is not None
    assert found.drawdown_r.p5 == Decimal(25)
    assert found.losing_streak.p50 == Decimal(25)
    assert found.negative_share == Decimal(1)


def test_the_same_seed_gives_the_same_answer_and_another_seed_another() -> None:
    trades = rs(*(["2", "-1", "-1", "0.5"] * 10))

    first = simulate(trades, paths=300, seed="a")
    again = simulate(trades, paths=300, seed="a")
    other = simulate(trades, paths=300, seed="b")

    assert first == again
    assert first != other


def test_the_percentiles_are_ordered_and_the_path_count_is_kept() -> None:
    found = simulate(rs(*(["3", "-1", "-1", "-1", "0.2"] * 8)), paths=500, seed="s")

    assert found is not None
    assert (found.paths, found.trades) == (500, 40)
    for spread in (found.drawdown_r, found.losing_streak, found.net_r):
        assert spread.p5 <= spread.p50 <= spread.p95 <= spread.p99
    # A method that loses 3 of 5 trades must see streaks of several losses in some path.
    assert found.losing_streak.p99 >= 3


def test_resampling_is_not_shuffling_the_final_r_moves() -> None:
    """Shuffling would keep the total at +2 on every path; drawing with replacement does not."""
    found = simulate(rs(*(["1", "-1"] * 10 + ["2"])), paths=500, seed="s")

    assert found is not None
    assert found.net_r.p5 < found.net_r.p95
    assert Decimal(0) < found.negative_share < Decimal(1)


def test_no_paths_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one path"):
        simulate(rs(*["1"] * 30), paths=0, seed="s")


# --- In blocks (01/10): runs of trades in a row, so losses that cluster are dealt together. ---


def clustered() -> list[Decimal]:
    """200 trades that make money, their losses all in runs of fifteen: a regime method."""
    return rs(*(["-1"] * 15 + ["1"] * 25) * 5)


def independent() -> list[Decimal]:
    """200 trades with no order to them, drawn once from a fixed generator."""
    draw = random.Random(7)  # noqa: S311 — test data, not a secret
    return rs(*(draw.choice(["1", "-1", "1.5", "-1"]) for _ in range(200)))


def test_the_default_block_is_the_cube_root_of_the_sample_and_never_under_two() -> None:
    assert [default_block_trades(n) for n in (1, 8, 20, 64, 150, 300, 1000)] == [
        2,
        2,
        3,
        4,
        5,
        7,
        10,
    ]


def test_a_block_fits_only_under_half_the_sample() -> None:
    assert block_fits(21, 10)
    assert not block_fits(20, 10)
    assert not block_fits(20, 11)


def test_in_blocks_the_same_seed_gives_the_same_answer_and_another_seed_another() -> None:
    first = simulate_in_blocks(clustered(), paths=300, seed="a")
    again = simulate_in_blocks(clustered(), paths=300, seed="a")
    other = simulate_in_blocks(clustered(), paths=300, seed="b")

    assert first == again
    assert first != other


def test_in_blocks_keeps_the_block_it_used_default_or_asked() -> None:
    by_default = simulate_in_blocks(clustered(), paths=100, seed="s")
    asked = simulate_in_blocks(clustered(), paths=100, seed="s", block_trades=12)

    assert by_default is not None
    assert asked is not None
    assert (by_default.block_trades, asked.block_trades) == (6, 12)
    assert (by_default.paths, by_default.trades) == (100, 200)
    found = simulate(clustered(), paths=100, seed="s")
    assert found is not None
    assert found.block_trades is None


def test_losses_that_come_in_runs_fall_deeper_in_blocks_than_trade_by_trade() -> None:
    """Trade by trade breaks the runs of fifteen losses up; in blocks they arrive whole."""
    one_by_one = simulate(clustered(), paths=1000, seed="s")
    in_blocks = simulate_in_blocks(clustered(), paths=1000, seed="s")

    assert one_by_one is not None
    assert in_blocks is not None
    assert in_blocks.drawdown_r.p95 > 2 * one_by_one.drawdown_r.p95
    assert in_blocks.losing_streak.p95 > one_by_one.losing_streak.p95


def test_trades_with_no_order_fall_about_as_deep_either_way() -> None:
    one_by_one = simulate(independent(), paths=1000, seed="s")
    in_blocks = simulate_in_blocks(independent(), paths=1000, seed="s")

    assert one_by_one is not None
    assert in_blocks is not None
    ratio = in_blocks.drawdown_r.p95 / one_by_one.drawdown_r.p95
    assert Decimal("0.8") <= ratio <= Decimal("1.25")


def test_in_blocks_every_trade_the_same_gives_one_answer_whatever_the_wrap() -> None:
    """The ring joins the last trade to the first: with one value there is nothing to tell."""
    found = simulate_in_blocks(rs(*["-1"] * 25), paths=100, seed="s", block_trades=4)

    assert found is not None
    assert found.drawdown_r.p5 == found.drawdown_r.p99 == Decimal(25)
    assert found.losing_streak.p5 == Decimal(25)
    assert found.negative_share == Decimal(1)


def test_in_blocks_too_few_trades_are_not_resampled_and_a_block_too_big_is_refused() -> None:
    assert simulate_in_blocks(rs(*["1"] * (MIN_TRADES - 1)), paths=100, seed="s") is None
    with pytest.raises(ValueError, match="half or more"):
        simulate_in_blocks(rs(*["1"] * 30), paths=100, seed="s", block_trades=15)
    with pytest.raises(ValueError, match="at least 2"):
        simulate_in_blocks(rs(*["1"] * 30), paths=100, seed="s", block_trades=1)
    with pytest.raises(ValueError, match="at least one path"):
        simulate_in_blocks(rs(*["1"] * 30), paths=0, seed="s")


def test_in_blocks_costs_about_what_trade_by_trade_costs() -> None:
    """2000 paths of 300 trades: measured 01/10 at ~0.1 s each way. A loose ceiling — the point is
    the same order, not a benchmark on a shared CI runner."""
    trades = independent() + independent()[:100]
    started = time.perf_counter()
    simulate(trades, paths=2000, seed="s")
    one_by_one = time.perf_counter() - started
    started = time.perf_counter()
    simulate_in_blocks(trades, paths=2000, seed="s")
    in_blocks = time.perf_counter() - started

    assert in_blocks < 3 * one_by_one + 0.05
