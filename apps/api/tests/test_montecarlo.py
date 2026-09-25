"""Monte Carlo of a run's trades — the cases whose answer is known without simulating."""

from decimal import Decimal

import pytest

from tradeforge_api.montecarlo import MIN_TRADES, simulate


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
