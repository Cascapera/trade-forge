"""Reading an asset class out of a symbol tree, and refusing to guess one."""

import pytest

from tradeforge_collector.classify import asset_class_from_path
from tradeforge_engine.domain import AssetClass


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Forex\\Majors\\EURUSD", AssetClass.FOREX),
        ("Stocks\\US\\AAPL", AssetClass.STOCK),
        ("Indices\\US500", AssetClass.INDEX),
        ("Crypto\\BTCUSD", AssetClass.CRYPTO),
        ("Futures\\CL", AssetClass.FUTURE),
        # Measured on this broker: its seven crypto symbols file under `Crypto Currency`, which
        # is the same word with a noun after it. The only root added to the map, and the reason
        # is that it *names* the class rather than correlating with it.
        ("Crypto Currency\\BTCUSD", AssetClass.CRYPTO),
    ],
)
def test_the_asset_class_is_read_from_the_symbol_tree(path: str, expected: AssetClass) -> None:
    assert asset_class_from_path(path) == expected


@pytest.mark.parametrize("path", ["CFDs\\XAUUSD", "Metals\\XAGUSD", "CFD\\Exotic\\WHATEVER"])
def test_a_path_that_names_no_class_returns_none_instead_of_guessing(path: str) -> None:
    """⚠️ The two roots that hold 17 of this broker's 84 symbols, and neither has an answer.

    `CFDs` says what the *contract* is, not what it is over; `Metals` names something the five
    members of `AssetClass` cannot express at all. `None` is not a failure here — it is the
    input to a question the screen asks, because the person launching a collection knows what
    `CFDs\\XAUUSD` is and the string does not.

    ⚠️ And the reason for refusing is narrower than this repo's backlog states. Nothing in the
    engine branches on the class — `costs.py`, `domain.py` and `protocols.py` each say there is
    no `if asset_class ==` anywhere, and P&L comes from tick size, tick value and contract size.
    What refuses is `instruments.asset_class`, a NOT NULL column with no member for "nobody
    decided". So this guards a fact from being invented, not an arithmetic from being wrong.
    """
    assert asset_class_from_path(path) is None


def test_the_separator_and_the_case_are_the_broker_s_business() -> None:
    """MT5 writes backslashes; anything that round-trips a path through a URL or a JSON blob
    may hand back forward slashes, and a broker is free to shout its roots."""
    assert asset_class_from_path("forex/majors/eurusd") == AssetClass.FOREX
    assert asset_class_from_path("  FOREX\\Majors\\EURUSD") == AssetClass.FOREX


def test_an_empty_path_is_undecidable_rather_than_an_error() -> None:
    """`symbol_info.path` is the empty string for "not set", exactly like `description` is.

    A symbol with no tree is a symbol nobody filed, which is precisely the case the screen
    exists to ask about — raising here would turn it into a job that fails on the host instead.
    """
    assert asset_class_from_path("") is None
