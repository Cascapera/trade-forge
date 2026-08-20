"""Reading an asset class out of a broker's symbol tree — and refusing to guess.

Pure, and here rather than in `mt5_source` for one reason: **the API needs this answer and
cannot ask MetaTrader for it.** The screen has to know *before* a collection is queued whether
the class is decidable, because "this path does not say" is a field to fill in, not an error to
show after a job has already failed on the host. `broker_symbols` already stores the path the
snapshot photographed, so the API applies the same function to the same string and asks up
front. Keeping it beside `MT5Source` would have meant importing, from Linux, a module whose own
docstring says it is never imported there.

⚠️ **A wrong class here is a wrong label, not wrong money — and that is worth stating because
the opposite is written down in this repo's own backlog.** The engine carries no `if
asset_class ==` anywhere (see `costs.py`, `domain.py`, `protocols.py`, which each say so): P&L
comes from `tick_size`, `tick_value` and `contract_size`, read straight off the terminal, and
the cost model is plugged into a run rather than derived from a class (ADR-07). What actually
depends on this value is `instruments.asset_class`, a NOT NULL column with five legal values.

So the refusal below is not protecting an arithmetic. It is refusing to write a fact nobody
established into a column that cannot hold "unknown".
"""

from tradeforge_engine.domain import AssetClass

__all__ = ["PATH_TO_ASSET_CLASS", "asset_class_from_path"]

# MT5 groups symbols in a tree: "Forex\\Majors\\EURUSD", "Stocks\\US\\AAPL".
#
# ⚠️ Every key is a word that *names* the class, never a word that merely correlates with it.
# Measured on this broker on 19/08/2026, its 84 symbols file under four roots: `Forex` (60),
# `CFDs` (14), `Crypto Currency` (7) and `Metals` (3). Only the third joins this map, and only
# because "crypto currency" and "crypto" are the same word — mapping `CFDs` would be inventing
# what the contract is over, and `Metals` has no honest answer among the five members at all.
PATH_TO_ASSET_CLASS: dict[str, AssetClass] = {
    "forex": AssetClass.FOREX,
    "stocks": AssetClass.STOCK,
    "shares": AssetClass.STOCK,
    "indices": AssetClass.INDEX,
    "indexes": AssetClass.INDEX,
    "futures": AssetClass.FUTURE,
    "crypto": AssetClass.CRYPTO,
    "crypto currency": AssetClass.CRYPTO,
}


def asset_class_from_path(path: str) -> AssetClass | None:
    """Read the asset class out of the symbol's tree path. `None` when it cannot tell.

    `None` is an answer with a caller: the collection screen turns it into a field the person
    launching the run fills in, because they know what a `CFDs\\XAUUSD` is and no string does.
    Guessing here would put that same value in the column with nobody having decided it.
    """
    head = path.replace("/", "\\").split("\\")[0].strip().lower()
    return PATH_TO_ASSET_CLASS.get(head)
