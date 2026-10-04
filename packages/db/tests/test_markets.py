"""The market a broker symbol belongs to — the symbol browser's tabs (02/10)."""

import pytest

from tradeforge_db.broker_symbols import MARKETS, market_of


@pytest.mark.parametrize(
    ("path", "symbol", "market"),
    [
        # ActivTrades, as synced on 02/10.
        (r"Forex\Majors\EURUSD", "EURUSD", "forex"),
        (r"Cryptocurrency\BTCUSD", "BTCUSD", "crypto"),
        (r"Cash Indices\Usa500", "Usa500", "indices"),
        (r"Metals\GOLD", "GOLD", "metals"),
        (r"Spot Energy\Brent", "Brent", "commodities"),
        (r"CFD Forward 1\3\UsaIndDec26", "UsaIndDec26", "futures"),
        (r"CFD UK Shares\UK Shares 1\BARC.UK", "BARC.UK", "stocks_other"),
        (r"CFD Swe\SAABB.SE", "SAABB.SE", "stocks_other"),
        # Other brokers' trees and tickers.
        (r"Crypto Currency\BTC\BTCUSD", "BTCUSD", "crypto"),
        (r"Stocks\US\AAPL", "AAPL", "stocks_us"),
        (r"Shares\NASDAQ\MSFT", "MSFT", "stocks_us"),
        (r"CFDs\AAPL.US", "AAPL.US", "stocks_us"),
        (r"Acoes\PETR4.SA", "PETR4.SA", "stocks_br"),
        (r"Stocks\Brazil\VALE3", "VALE3", "stocks_br"),
        (r"Stocks\B3\ITUB4", "ITUB4", "stocks_br"),
        (None, "WEIRD", "other"),
    ],
)
def test_each_symbol_lands_in_its_market(path: str | None, symbol: str, market: str) -> None:
    assert market_of(path, symbol) == market


def test_every_market_answered_is_a_tab() -> None:
    keys = {key for key, _label in MARKETS}
    assert {"forex", "crypto", "indices", "stocks_us", "stocks_br", "other"} <= keys
    assert len(keys) == len(MARKETS)
