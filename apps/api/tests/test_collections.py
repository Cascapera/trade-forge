"""What a collection request accepts and refuses, with no database in sight.

The queries and the 202 are proved against a real Postgres in `test_collections_integration.py`.
What is proved here is the part of the endpoint that is a *decision*: which windows are legal,
and which instants are legal, and what happens to a symbol nobody can classify.
"""

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from tradeforge_api.schemas import (
    MAX_COLLECTION_SYMBOLS,
    CollectionOut,
    CreateCollectionRequest,
)
from tradeforge_db.models import BacktestStatus, Collection
from tradeforge_engine.domain import AssetClass


def request(**fields: object) -> CreateCollectionRequest:
    payload: dict[str, object] = {
        "items": [{"symbol": "EURUSD"}],
        "timeframe": "H1",
        "date_from": "2020-01-01T00:00:00Z",
        "date_to": "2021-01-01T00:00:00Z",
    }
    payload.update(fields)
    return CreateCollectionRequest.model_validate(payload)


def items(*symbols: str) -> list[dict[str, str]]:
    """`items` for a batch of plain symbols, none of them carrying a class."""
    return [{"symbol": symbol} for symbol in symbols]


class TestTheWindow:
    def test_an_ordinary_window_is_accepted(self) -> None:
        assert request().date_from == dt.datetime(2020, 1, 1, tzinfo=dt.UTC)

    def test_a_window_that_runs_backwards_is_refused(self) -> None:
        """Refused at the edge rather than by `year_slices` inside the job, because the caller
        who can fix it is the one holding the form."""
        with pytest.raises(ValidationError, match="before"):
            request(date_from="2021-01-01T00:00:00Z", date_to="2020-01-01T00:00:00Z")

    def test_a_window_of_a_single_instant_is_legal(self) -> None:
        """⚠️ Not the same as backwards, and a `<=` written as `<` would refuse it. A range of
        one instant is a strange thing to ask for and a perfectly answerable one."""
        moment = "2020-06-01T12:00:00Z"

        assert request(date_from=moment, date_to=moment).date_to.hour == 12

    def test_an_instant_with_no_timezone_is_refused(self) -> None:
        """⚠️ The failure this project has already paid for once.

        The window reaches MetaTrader, which speaks the server's clock — the collector shifts
        UTC into it on the way in and back out on the way home. A naive instant would be
        shifted anyway and every bar in the file would land hours out of place, with nothing
        raised. The first real backfill on this project wrote candles 62 hours displaced.
        """
        with pytest.raises(ValidationError, match="timezone"):
            request(date_from="2020-01-01T00:00:00")

    def test_a_non_utc_timezone_is_accepted_and_kept_as_an_instant(self) -> None:
        """An offset is a timezone; the guard is against *absence*, not against non-UTC.
        Refusing `+03:00` would reject a perfectly determined instant for looking unfamiliar."""
        parsed = request(date_from="2020-01-01T03:00:00+03:00")

        assert parsed.date_from == dt.datetime(2020, 1, 1, tzinfo=dt.UTC)

    def test_a_timeframe_the_dsl_does_not_define_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            request(timeframe="M7")


class TestTheAssetClass:
    def test_it_is_absent_by_default_rather_than_defaulted(self) -> None:
        """⚠️ `None` means *the path already says*, and the router reads it that way. A default
        of `forex` here would file every unclassifiable CFD as a currency pair, silently."""
        assert request().items[0].asset_class is None

    def test_a_supplied_class_is_kept(self) -> None:
        parsed = request(items=[{"symbol": "BTCUSD", "asset_class": "crypto"}])

        assert parsed.items[0].asset_class is not None

    def test_a_class_the_system_has_no_member_for_is_refused(self) -> None:
        """`metal` is what XAUUSD honestly is, and there is no such member. Refusing here is
        better than storing a string the engine's enum cannot round-trip."""
        with pytest.raises(ValidationError):
            request(items=[{"symbol": "XAUUSD", "asset_class": "metal"}])

    def test_the_class_belongs_to_its_own_symbol_and_not_to_the_batch(self) -> None:
        """⚠️ The reason `items` is a list of objects rather than a `symbols` list beside an
        `asset_classes` list: two parallel arrays can fall out of step and no schema can forbid
        it, and the symbol that then gets catalogued as the wrong kind fails silently.

        XAUUSD is a future and BTCUSD is crypto; a batch-wide field would have to lie about one.
        """
        parsed = request(
            items=[
                {"symbol": "XAUUSD", "asset_class": "future"},
                {"symbol": "BTCUSD", "asset_class": "crypto"},
                {"symbol": "EURUSD"},
            ]
        )

        assert [(item.symbol, item.asset_class) for item in parsed.items] == [
            ("XAUUSD", AssetClass.FUTURE),
            ("BTCUSD", AssetClass.CRYPTO),
            ("EURUSD", None),
        ]


class TestHowManySymbols:
    """The ceiling and the floor, each tested on both sides of the line.

    ⚠️ Twenty accepted **and** twenty-one refused, one accepted **and** none refused. A test
    that only proves the refusal cannot tell `>` from `>=`, and the off-by-one it misses is the
    one that makes the documented limit a lie.
    """

    def test_one_symbol_is_a_batch_of_one(self) -> None:
        """The single-symbol collection did not go away; it is the degenerate batch."""
        assert [item.symbol for item in request().items] == ["EURUSD"]

    def test_no_symbols_at_all_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            request(items=[])

    def test_the_ceiling_itself_is_accepted(self) -> None:
        assert (
            len(request(items=items(*(f"SYM{n:02d}" for n in range(MAX_COLLECTION_SYMBOLS)))).items)
            == 20
        )

    def test_one_past_the_ceiling_is_refused(self) -> None:
        """A list the client controls needs a ceiling for the same reason every numeric query
        parameter does: without one, a single POST enqueues as many jobs as the caller likes."""
        with pytest.raises(ValidationError):
            request(items=items(*(f"SYM{n:02d}" for n in range(MAX_COLLECTION_SYMBOLS + 1))))

    def test_the_same_symbol_twice_is_refused_rather_than_deduplicated(self) -> None:
        """⚠️ Refused, not quietly collapsed. Two collections of EURUSD H1 over the same window
        are the same work done twice, and a caller who sent the duplicate by accident learns
        nothing from being silently corrected — the response would simply disagree with the
        list they sent."""
        with pytest.raises(ValidationError, match="EURUSD"):
            request(items=items("EURUSD", "GBPUSD", "EURUSD"))

    def test_the_refusal_names_every_repeated_symbol_not_just_the_first(self) -> None:
        with pytest.raises(ValidationError, match="EURUSD, GBPUSD"):
            request(items=items("EURUSD", "GBPUSD", "EURUSD", "GBPUSD"))


class TestWhatComesBack:
    def test_a_queued_request_reports_no_outcome_yet(self) -> None:
        """⚠️ `candles` is `None` and not `0` before the work runs, and the screen renders the
        two differently: zero is the claim that the broker had nothing, which is a finished
        answer, and a request that has not started has made no claim at all."""
        row = Collection(
            # ⚠️ Spelled out because `default=uuid.uuid4` fires when the row is **written**, not
            # when the object is constructed — the same trap `test_symbols.py` documents for
            # `visible`. Every row the API serialises came out of a NOT NULL primary key.
            id=uuid.uuid4(),
            symbol="EURUSD",
            timeframe="H1",
            date_from=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
            date_to=dt.datetime(2021, 1, 1, tzinfo=dt.UTC),
            status=BacktestStatus.QUEUED,
            years_done=0,
            years_total=2,
            requested_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
        )

        out = CollectionOut.model_validate(row)

        assert out.status == "queued"
        assert out.candles is None
        assert out.gaps is None
        assert out.error is None
        assert (out.years_done, out.years_total) == (0, 2)

    def test_a_finished_request_reports_the_window_it_asked_for(self) -> None:
        """⚠️ The requested window, not the coverage. Asking for 2015 on a symbol listed in 2018
        is ordinary, and a body that reported only what was found could never explain why the
        result is shorter than what is on screen."""
        row = Collection(
            id=uuid.uuid4(),
            symbol="BTCUSD",
            timeframe="H1",
            date_from=dt.datetime(2015, 1, 1, tzinfo=dt.UTC),
            date_to=dt.datetime(2023, 1, 1, tzinfo=dt.UTC),
            status=BacktestStatus.DONE,
            years_done=9,
            years_total=9,
            candles=26366,
            gaps=234,
            requested_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
        )

        out = CollectionOut.model_validate(row)

        assert out.date_from.year == 2015, "the request, even though the data starts later"
        assert out.candles == 26366
