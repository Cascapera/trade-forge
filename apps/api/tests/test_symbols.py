"""The one thing the search response decides, with no database in sight.

`/symbols/search` is a query and a serialisation. The query is proved against a real Postgres
in `test_symbols_integration.py`; what is proved here is the shape of the answer — specifically
that it keeps apart two situations which both produce an empty list and mean opposite things.
"""

import datetime as dt

import pytest
from pydantic import TypeAdapter, ValidationError

from tradeforge_api.schemas import SymbolHistoryOut, SymbolSearchOut, Timeframe
from tradeforge_db.models import BrokerSymbol, SymbolHistory
from tradeforge_engine.domain import AssetClass

SYNCED_AT = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)


def _row(symbol: str, **fields: object) -> BrokerSymbol:
    """A row as the query would hand it back.

    ⚠️ `visible` is spelled out because column defaults — `default` and `server_default` alike
    — fire when a row is **written**, not when the object is constructed. An unflushed
    `BrokerSymbol` therefore has `visible = None`, which is a fact about building rows by hand
    and not about the data: every row the API actually serialises came back out of a NOT NULL
    column. Leaving it off here would fail the serialiser for a reason production never has.
    """
    fields.setdefault("visible", False)
    return BrokerSymbol(symbol=symbol, synced_at=SYNCED_AT, **fields)


def test_no_match_and_no_catalogue_are_told_apart() -> None:
    """⚠️ Both hand back zero symbols, and they are opposite problems.

    "Nothing starts with `zzz`" means type fewer letters. "Nobody has ever synced this broker"
    means the list does not exist and searching it harder will not help. A response that
    flattened the two would have the screen tell a user with no catalogue that their search
    found nothing — true, and the least useful true sentence available.

    It is the same distinction the instruments table already makes between a NULL spread and a
    zero one, one layer up.
    """
    no_match = SymbolSearchOut.build(symbols=[], server="MetaQuotes-Demo", synced_at=SYNCED_AT)
    no_catalogue = SymbolSearchOut.build(symbols=[], server=None, synced_at=None)

    assert no_match.symbols == []
    assert no_catalogue.symbols == []
    # The list is identical; only the provenance separates them.
    assert no_match.snapshot is not None
    assert no_catalogue.snapshot is None


def test_a_snapshot_from_a_server_that_did_not_name_itself_is_still_a_snapshot() -> None:
    """⚠️ The case a truthiness check would get wrong.

    A terminal that is running but not logged in reports no server name, so `server` is `None`
    while `synced_at` is a real instant — the sync genuinely happened. Deciding "is there a
    catalogue?" from the *name* would call that no catalogue at all and send the user to
    re-sync something that is already there.
    """
    built = SymbolSearchOut.build(symbols=[_row("EURUSD")], server=None, synced_at=SYNCED_AT)

    assert built.snapshot is not None
    assert built.snapshot.server is None
    assert built.snapshot.synced_at == SYNCED_AT


def test_a_result_carries_the_symbol_and_where_the_broker_files_it() -> None:
    built = SymbolSearchOut.build(
        symbols=[
            _row(
                "EURUSD",
                description="Euro vs US Dollar",
                path="Forex\\Majors\\EURUSD",
                digits=5,
                visible=True,
            )
        ],
        server="MetaQuotes-Demo",
        synced_at=SYNCED_AT,
    )

    found = built.symbols[0]
    assert found.symbol == "EURUSD"
    assert found.description == "Euro vs US Dollar"
    assert found.path == "Forex\\Majors\\EURUSD"
    assert found.digits == 5
    assert found.visible is True


def test_a_result_does_not_pretend_to_know_how_to_price_the_symbol() -> None:
    """⚠️ A search result is not an `InstrumentOut`, and must not look like one.

    Most symbols in this list have never been collected, so nothing has ever read their tick
    value or decided their asset class. Serialising zeroes for those fields would hand the
    screen numbers that price a position — the exact numbers that, wrong, make a P&L wrong by a
    constant factor and look like a very good strategy.
    """
    built = SymbolSearchOut.build(symbols=[_row("EURUSD")], server=None, synced_at=SYNCED_AT)

    serialised = built.symbols[0].model_dump()
    assert "tick_value" not in serialised
    assert "tick_size" not in serialised
    assert "asset_class" not in serialised


def probe(**patch: object) -> SymbolHistory:
    """A stored probe, with EURUSD D1's measured numbers as the default."""
    fields: dict[str, object] = {
        "symbol": "EURUSD",
        "timeframe": "D1",
        "oldest": dt.datetime(1971, 1, 3, tzinfo=dt.UTC),
        "bar_count": 14_343,
        "terminal_maxbars": 100_000_000,
        "bar_count_is_a_ceiling": False,
        "last_fabricated": 1972,
        "first_measured_cost": 2009,
        "probed_at": SYNCED_AT,
    }
    fields.update(patch)
    return SymbolHistory(**fields)


class TestWhatTheScreenIsToldAboutHistory:
    def test_the_later_floor_decides_where_a_window_may_start(self) -> None:
        """Filler stops in 1972 and typed costs in 2009, so the honest start is 2009.

        A run is only as trustworthy as its weaker half; picking the earlier floor would hand
        somebody thirty-seven years of prices costed with a number that was typed.
        """
        assert SymbolHistoryOut.build(probe()).usable_from == dt.datetime(2009, 1, 1, tzinfo=dt.UTC)

    def test_the_year_after_the_filler_is_the_first_usable_one(self) -> None:
        # ⚠️ Off by one here puts a year of made-up bars inside every default window.
        found = SymbolHistoryOut.build(probe(last_fabricated=1972, first_measured_cost=None))

        assert found.usable_from == dt.datetime(1973, 1, 1, tzinfo=dt.UTC)

    def test_a_floor_older_than_the_data_does_not_invent_history(self) -> None:
        """⚠️ A lower bound on trust, never a claim that bars exist.

        BTCUSD measured exactly this: costs measured since 2022 and no data before May of it.
        Returning the floor would name a date the terminal cannot reach.
        """
        found = SymbolHistoryOut.build(
            probe(
                oldest=dt.datetime(2022, 5, 10, tzinfo=dt.UTC),
                last_fabricated=None,
                first_measured_cost=2022,
            )
        )

        assert found.usable_from == dt.datetime(2022, 5, 10, tzinfo=dt.UTC)

    def test_with_no_floors_known_it_falls_back_to_the_oldest_bar(self) -> None:
        found = SymbolHistoryOut.build(probe(last_fabricated=None, first_measured_cost=None))

        assert found.usable_from == dt.datetime(1971, 1, 3, tzinfo=dt.UTC)

    def test_a_series_at_the_terminal_ceiling_says_whose_limit_it_is(self) -> None:
        # Measured before `maxbars` was raised: 100000 on M1, M5, M15 and H1 alike. The same
        # round number four times is a setting, not a broker.
        found = SymbolHistoryOut.build(probe(bar_count=100_000, terminal_maxbars=100_000))

        assert found.capped_by_terminal is True

    def test_an_unknown_ceiling_never_reports_a_cap(self) -> None:
        """⚠️ Zero would otherwise make every series look capped, since any count is >= 0."""
        found = SymbolHistoryOut.build(probe(bar_count=14_343, terminal_maxbars=0))

        assert found.capped_by_terminal is False

    def test_the_four_bounds_survive_to_the_wire(self) -> None:
        """⚠️ They are independent, and a reader can only act on the one that binds them.

        The ceiling is fixed in a settings dialog, the filler by starting later, the costs by
        not trusting them. A response that shipped only `usable_from` would leave all three
        actions unavailable while looking complete.
        """
        body = SymbolHistoryOut.build(probe()).model_dump()

        assert body["bar_count"] == 14_343
        assert body["terminal_maxbars"] == 100_000_000
        assert body["last_fabricated"] == 1972
        assert body["first_measured_cost"] == 2009

    def test_a_count_that_is_really_the_probes_own_bound_is_flagged(self) -> None:
        # Seen for real at exactly 10,000,000 once maxbars was raised — the search ceiling, not
        # a measurement, and the one number somebody would size a window from.
        found = SymbolHistoryOut.build(probe(bar_count=10_000_000, bar_count_is_a_ceiling=True))

        assert found.bar_count_is_a_ceiling is True


class TestTheTimeframeAtTheEdge:
    @pytest.mark.parametrize("timeframe", ["M1", "M5", "H1", "D1", "W1"])
    def test_the_dsl_decides_what_is_legal(self, timeframe: str) -> None:
        assert TypeAdapter(Timeframe).validate_python(timeframe) == timeframe

    def test_a_timeframe_the_dsl_does_not_define_is_refused(self) -> None:
        """⚠️ 422 rather than a 404 saying "not probed yet".

        An unknown timeframe would otherwise miss the lookup and come back as "nobody has probed
        this", sending somebody to press a probe button for a series that cannot exist.
        """
        with pytest.raises(ValidationError, match="unknown timeframe"):
            TypeAdapter(Timeframe).validate_python("M2")

    def test_a_nul_byte_is_refused_here_too(self) -> None:
        # The same guard `?q=` needed, and for the same reason: this reaches a text column.
        with pytest.raises(ValidationError):
            TypeAdapter(Timeframe).validate_python(chr(0))


class TestTheClassThePathDecides:
    """The field that lets the screen ask the right question before anything is sent.

    ⚠️ **The reason it lives on the API rather than in the browser.** The rule that turns a
    tree path into a class is `classify.asset_class_from_path`, tested in the collector and
    already imported by the collections route. Reimplementing it in TypeScript would put
    business logic in two languages, and the copies would diverge on the first symbol the
    broker files under a new root — the screen would offer to collect it and the API would
    refuse, with nothing to explain the disagreement.
    """

    def test_a_path_that_names_the_class_reports_it(self) -> None:
        built = SymbolSearchOut.build(
            symbols=[_row("EURUSD", path="Forex\\Majors\\EURUSD")],
            server=None,
            synced_at=SYNCED_AT,
        )

        assert built.symbols[0].asset_class_from_path == AssetClass.FOREX

    def test_a_path_that_names_no_class_reports_none(self) -> None:
        """⚠️ 24 of this broker's 84 symbols. `None` is the whole point of the field: it is
        what tells the screen to ask, rather than to send a request the API will refuse."""
        built = SymbolSearchOut.build(
            symbols=[_row("XAUUSD", path="CFDs\\Metals\\XAUUSD")],
            server=None,
            synced_at=SYNCED_AT,
        )

        assert built.symbols[0].asset_class_from_path is None

    def test_a_symbol_with_no_path_at_all_reports_none(self) -> None:
        """MT5 returns the empty string for "not set", which the snapshot stores as NULL. No
        path is no evidence, which is the same answer as an unrecognised root."""
        built = SymbolSearchOut.build(symbols=[_row("EURUSD")], server=None, synced_at=SYNCED_AT)

        assert built.symbols[0].asset_class_from_path is None

    def test_crypto_currency_is_read_as_crypto(self) -> None:
        """Seven of this broker's symbols file under a root that is two words.

        ⚠️ **Documented equivalent mutant.** Reading only the first word —
        `asset_class_from_path(path.split(" ")[0])` — passes this test, because
        `PATH_TO_ASSET_CLASS` holds *both* `crypto` and `crypto currency`. No scenario
        separates the two while that is true, so this asserts the answer the screen depends
        on rather than pretending to pin the parsing.

        The parsing itself belongs to `test_classify.py`, which already covers this exact
        path. What is proved here is that the value survives the trip to the response.
        """
        built = SymbolSearchOut.build(
            symbols=[_row("BTCUSD", path="Crypto Currency\\BTCUSD")],
            server=None,
            synced_at=SYNCED_AT,
        )

        assert built.symbols[0].asset_class_from_path == AssetClass.CRYPTO

    def test_it_reaches_the_wire_and_is_not_merely_a_python_attribute(self) -> None:
        """⚠️ A computed field that is not serialised is invisible to the only caller that
        wants it. Asserting the attribute alone would pass with `@computed_field` missing —
        the property would still answer in Python while the JSON carried nothing.
        """
        built = SymbolSearchOut.build(
            symbols=[
                _row("EURUSD", path="Forex\\Majors\\EURUSD"),
                _row("XAUUSD", path="CFDs\\Metals\\XAUUSD"),
            ],
            server=None,
            synced_at=SYNCED_AT,
        )

        on_the_wire = built.model_dump(mode="json")["symbols"]

        assert [row["asset_class_from_path"] for row in on_the_wire] == ["forex", None]

    def test_the_symbol_still_reports_the_path_it_was_derived_from(self) -> None:
        """Derived, never a replacement. The screen shows the path when it has to ask, because
        `CFDs\\Metals\\XAUUSD` is what tells a person the answer is `future`."""
        built = SymbolSearchOut.build(
            symbols=[_row("XAUUSD", path="CFDs\\Metals\\XAUUSD")],
            server=None,
            synced_at=SYNCED_AT,
        )

        found = built.symbols[0]
        assert found.path == "CFDs\\Metals\\XAUUSD"
        assert found.asset_class_from_path is None
